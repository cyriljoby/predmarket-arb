"""Unit tests for Kalshi order-book normalization (the YES/NO derivation)."""

import asyncio
from datetime import UTC, datetime
from typing import ClassVar

from pmarb.feeds.kalshi import (
    KalshiFeed,
    _asks_from_bids,
    _line_event,
    normalize_orderbook,
)
from pmarb.models import PriceLevel

OBSERVED = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
# Sentinel for "the venue stops sending but never closes the socket" — the
# failure that cost the 30-day run 15.1h in one stretch.
_SILENCE = object()
MARKET = {
    "ticker": "TEST-1",
    "title": "Will X happen?",
    "expiration_time": "2026-09-01T00:00:00Z",
}
# YES bids at 0.40/0.42, NO bids at 0.50/0.55 (strings, ascending — as Kalshi sends).
ORDERBOOK = {
    "yes_dollars": [["0.40", "100"], ["0.42", "50"]],
    "no_dollars": [["0.50", "200"], ["0.55", "80"]],
}


class TestYesNoDerivation:
    def test_yes_ask_comes_from_no_bids(self):
        m = normalize_orderbook(MARKET, ORDERBOOK, OBSERVED)
        # NO bids 0.50/0.55 -> YES asks at (1 - p) = 0.50/0.45, cheapest first.
        assert m.yes_depth == (PriceLevel(0.45, 80.0), PriceLevel(0.50, 200.0))
        assert m.yes_ask == 0.45

    def test_no_ask_comes_from_yes_bids(self):
        m = normalize_orderbook(MARKET, ORDERBOOK, OBSERVED)
        # YES bids 0.40/0.42 -> NO asks at (1 - p) = 0.60/0.58, cheapest first.
        assert m.no_depth == (PriceLevel(0.58, 50.0), PriceLevel(0.60, 100.0))
        assert m.no_ask == 0.58

    def test_reference_bids_are_best_of_each_side(self):
        m = normalize_orderbook(MARKET, ORDERBOOK, OBSERVED)
        assert m.yes_bid == 0.42  # highest YES bid
        assert m.no_bid == 0.55   # highest NO bid


class TestInvariants:
    def test_depth_is_sorted_cheapest_first(self):
        m = normalize_orderbook(MARKET, ORDERBOOK, OBSERVED)
        assert list(m.yes_depth) == sorted(m.yes_depth, key=lambda lvl: lvl.price)
        assert list(m.no_depth) == sorted(m.no_depth, key=lambda lvl: lvl.price)

    def test_asks_from_bids_handles_unsorted_input(self):
        # Even if a venue ever sends bids out of order, asks come out sorted.
        asks = _asks_from_bids([["0.55", "80"], ["0.50", "200"]])
        assert asks == (PriceLevel(0.45, 80.0), PriceLevel(0.50, 200.0))


class TestMetadata:
    def test_identity_and_question(self):
        m = normalize_orderbook(MARKET, ORDERBOOK, OBSERVED)
        assert m.id == "kalshi:TEST-1"
        assert m.platform == "kalshi"
        assert m.question == "Will X happen?"

    def test_resolution_date_is_tz_aware(self):
        m = normalize_orderbook(MARKET, ORDERBOOK, OBSERVED)
        assert m.resolution_date.tzinfo is not None

    def test_empty_book_yields_no_depth(self):
        m = normalize_orderbook(MARKET, {}, OBSERVED)
        assert m.yes_depth == ()
        assert m.no_depth == ()
        assert m.yes_ask is None
        assert m.no_ask is None


class TestPagination:
    @staticmethod
    def _mk(ticker):
        return {
            "ticker": ticker,
            "title": "q",
            "expiration_time": "2026-09-01T00:00:00Z",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.45",
        }

    def _feed_returning(self, pages):
        """A KalshiFeed whose _get yields the given pages in order, recording
        the cursor param it was called with each time."""
        feed = KalshiFeed.__new__(KalshiFeed)  # skip __init__; no session needed
        seen_cursors = []

        async def fake_get(path, params=None):
            seen_cursors.append((params or {}).get("cursor"))
            return pages[len(seen_cursors) - 1]

        feed._get = fake_get
        return feed, seen_cursors

    def test_walks_all_pages_until_cursor_empty(self):
        pages = [
            {"events": [{"markets": [self._mk("A")]}], "cursor": "c1"},
            {"events": [{"markets": [self._mk("B")]}], "cursor": "c2"},
            {"events": [{"markets": [self._mk("C")]}], "cursor": ""},
        ]
        feed, seen = self._feed_returning(pages)
        markets = asyncio.run(feed.fetch_markets())
        assert [m.id for m in markets] == ["kalshi:A", "kalshi:B", "kalshi:C"]
        assert seen == [None, "c1", "c2"]  # first page no cursor, then follows it

    def test_stops_on_repeated_cursor(self):
        # A cursor that never advances must not loop forever.
        same_page = {"events": [{"markets": [self._mk("A")]}], "cursor": "stuck"}
        feed, seen = self._feed_returning([same_page] * 10)
        markets = asyncio.run(feed.fetch_markets())
        assert len(seen) == 2  # one advance, then detects the repeat and stops
        assert len(markets) == 2

    def test_filters_out_mve_and_unquoted(self):
        bad = [
            {"ticker": "KXMVE-1", "title": "q",
             "expiration_time": "2026-09-01T00:00:00Z",
             "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.45"},          # MVE
            {"ticker": "OK-1", "title": "q", "expiration_time": "2026-09-01T00:00:00Z",
             "yes_bid_dollars": None, "yes_ask_dollars": None},             # no quote
        ]
        page = {"events": [{"markets": [*bad, self._mk("GOOD")]}], "cursor": ""}
        feed, _ = self._feed_returning([page])
        markets = asyncio.run(feed.fetch_markets())
        assert [m.id for m in markets] == ["kalshi:GOOD"]


class TestStreamBooks:
    """Offline test of the WS delta bookkeeping (mocked socket)."""

    class _FakeWS:
        def __init__(self, messages):
            self._it = iter(messages)
            self.sent = []

        async def send(self, m):
            self.sent.append(m)

        async def recv(self):
            import websockets
            try:
                msg = next(self._it)
            except StopIteration:
                raise websockets.ConnectionClosed(None, None) from None
            if msg is _SILENCE:
                await asyncio.sleep(3600)   # never arrives; the read must time out
            return msg

    class _FakeConnect:
        def __init__(self, ws):
            self._ws = ws

        async def __aenter__(self):
            return self._ws

        async def __aexit__(self, *a):
            return False

    def _run(self, monkeypatch, messages, idle_timeout=180.0):
        import json

        import websockets

        from pmarb.feeds import kalshi as kmod

        market = kmod._market_metadata(
            {"ticker": "T", "title": "Q", "close_time": "2026-09-01T00:00:00Z"},
            OBSERVED,
        )
        ws = self._FakeWS([m if m is _SILENCE else json.dumps(m)
                           for m in messages])
        self._ws_after_run = ws   # so a test can assert on what was re-sent
        monkeypatch.setattr(kmod.websockets, "connect",
                            lambda *a, **k: self._FakeConnect(ws))
        monkeypatch.setattr(kmod, "kalshi_headers", lambda *a, **k: {})
        feed = KalshiFeed.__new__(KalshiFeed)
        feed._creds = object()

        async def collect():
            out = []
            try:
                async for m in feed.stream_books([market], reconnect=False,
                                                 idle_timeout=idle_timeout):
                    out.append(m)
            except websockets.ConnectionClosed:
                pass
            return out

        return asyncio.run(collect())

    def test_snapshot_then_delta_maintains_book(self, monkeypatch):
        out = self._run(monkeypatch, [
            {"type": "subscribed", "id": 1, "msg": {"sid": 1}},  # ignored
            {"type": "orderbook_snapshot", "seq": 1, "msg": {
                "market_ticker": "T",
                "yes_dollars_fp": [["0.60", "100.0"]],   # YES bid -> NO ask 0.40 x100
                "no_dollars_fp": [["0.30", "200.0"]],    # NO bid  -> YES ask 0.70 x200
            }},
            {"type": "orderbook_delta", "seq": 2, "msg": {
                "market_ticker": "T", "side": "no",
                "price_dollars": "0.30", "delta_fp": "-50.0",  # NO bid 200 -> 150
            }},
        ])
        assert len(out) == 2
        # snapshot: asks derived from the opposite side's bids
        assert out[0].yes_ask == 0.70 and out[0].no_ask == 0.40
        assert out[0].yes_depth[0].size == 200.0
        # delta shrank the NO bid -> YES ask size drops to 150
        assert out[1].yes_depth[0].price == 0.70
        assert out[1].yes_depth[0].size == 150.0

    def test_delta_removing_a_level_drops_it(self, monkeypatch):
        out = self._run(monkeypatch, [
            {"type": "orderbook_snapshot", "seq": 1, "msg": {
                "market_ticker": "T",
                "yes_dollars_fp": [], "no_dollars_fp": [["0.30", "40.0"]],
            }},
            {"type": "orderbook_delta", "seq": 2, "msg": {
                "market_ticker": "T", "side": "no",
                "price_dollars": "0.30", "delta_fp": "-40.0",  # zeroes the level
            }},
        ])
        assert out[0].yes_depth[0].size == 40.0
        assert out[1].yes_depth == ()  # level removed -> empty ask ladder

    def test_every_streamed_book_carries_its_receipt_instant(self, monkeypatch):
        # Detection latency is measured from this stamp. A book that arrives
        # without one is invisible to the survival curve, and a stamp taken
        # after the parse would quietly exclude the parse from the measurement.
        import time
        before = time.monotonic()
        out = self._run(monkeypatch, [
            {"type": "orderbook_snapshot", "seq": 1, "msg": {
                "market_ticker": "T",
                "yes_dollars_fp": [["0.60", "100.0"]], "no_dollars_fp": [],
            }},
        ])
        after = time.monotonic()
        assert out[0].received_mono is not None
        assert before <= out[0].received_mono <= after

    def test_a_silent_socket_is_torn_down_and_resubscribed(self, monkeypatch):
        # The 30-day run's dominant failure: the host suspends, the sockets go
        # half-open, and on wake the read blocks on a connection nothing will
        # ever write to again. Nothing raises, so the reconnect handler below
        # never fires. Only the ABSENCE of data reveals it.
        out = self._run(monkeypatch, [
            {"type": "orderbook_snapshot", "seq": 1, "msg": {
                "market_ticker": "T",
                "yes_dollars_fp": [], "no_dollars_fp": [["0.30", "40.0"]],
            }},
            _SILENCE,
        ], idle_timeout=0.05)
        assert len(out) == 1                      # the pre-silence snapshot
        # and the stream did not simply end: it reconnected and resubscribed,
        # which is what forces fresh snapshots after a gap.
        assert len(self._ws_after_run.sent) == 2

    def test_seq_gap_stops_the_stream(self, monkeypatch):
        # snapshot seq=1, then a delta at seq=5 (gap) -> break before yielding it
        out = self._run(monkeypatch, [
            {"type": "orderbook_snapshot", "seq": 1, "msg": {
                "market_ticker": "T",
                "yes_dollars_fp": [], "no_dollars_fp": [["0.30", "40.0"]],
            }},
            {"type": "orderbook_delta", "seq": 5, "msg": {
                "market_ticker": "T", "side": "no",
                "price_dollars": "0.30", "delta_fp": "-10.0",
            }},
        ])
        assert len(out) == 1  # only the snapshot; gap broke the loop before the delta


class TestLineExtraction:
    """Kalshi's own title names neither team on a total ("Over 35.5 points
    scored") — the enclosing EVENT title is the only place they appear, which
    is why discovery has to pass it through."""

    @staticmethod
    def _market(**over):
        base = {"ticker": "KXNCAAFTOTAL-26SEP12DELVAN-36",
                "title": "Over 35.5 points scored",
                "yes_sub_title": "Over 35.5 points scored",
                "floor_strike": 35.5, "strike_type": "greater",
                "close_time": "2026-09-14T20:15:00Z"}
        base.update(over)
        return base

    EVENT: ClassVar[dict] = {"title": "Delaware vs Vanderbilt: Total Points"}

    def test_total_takes_its_teams_from_the_event_title(self):
        le = _line_event(self._market(), self.EVENT)
        assert le.kind == "total" and le.line == 35.5 and le.league == "cfb"
        assert le.competitors == ("Delaware", "Vanderbilt")
        assert le.yes_team == ""          # YES is Over, not a team

    def test_spread_takes_its_yes_team_from_the_subtitle(self):
        le = _line_event(self._market(
            ticker="KXNCAAFSPREAD-26SEP12DELVAN-VAN42",
            title="Vanderbilt wins by over 41.5 points",
            yes_sub_title="Vanderbilt wins by over 41.5 points",
            floor_strike=41.5), {"title": "Delaware vs Vanderbilt: Spread"})
        assert le.kind == "spread" and le.yes_team == "Vanderbilt"
        # Kalshi only ever writes the laying side.
        assert le.yes_favored is True

    def test_without_the_event_there_is_no_identity(self):
        # The regression that hid this whole market class: discovery HAS the
        # event and dropped it, so totals had no team names at all.
        assert _line_event(self._market(), None) is None

    def test_a_spread_whose_side_cannot_be_read_is_refused(self):
        assert _line_event(self._market(
            ticker="KXNCAAFSPREAD-26SEP12DELVAN-VAN42",
            yes_sub_title="Something unparseable"),
            {"title": "Delaware vs Vanderbilt: Spread"}) is None

    def test_sub_period_series_are_not_full_game_lines(self):
        for t in ("KXNCAAF1HTOTAL-26SEP12DELVAN-36",
                  "KXNFL2QSPREAD-26SEP14DENKC-KC7",
                  "KXNCAAFTEAMTOTAL-26SEP12DELVAN-21",
                  "KXMLBINNINGTOTAL-26SEP0618WSHLAD-2"):
            assert _line_event(self._market(ticker=t), self.EVENT) is None

    def test_a_non_greater_strike_is_refused(self):
        assert _line_event(
            self._market(strike_type="less"), self.EVENT) is None

"""Unit tests for Kalshi order-book normalization (the YES/NO derivation)."""

import asyncio
from datetime import datetime, timezone

from pmarb.feeds.kalshi import KalshiFeed, _asks_from_bids, normalize_orderbook
from pmarb.models import PriceLevel

OBSERVED = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
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
            {"ticker": "KXMVE-1", "title": "q", "expiration_time": "2026-09-01T00:00:00Z",
             "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.45"},          # MVE
            {"ticker": "OK-1", "title": "q", "expiration_time": "2026-09-01T00:00:00Z",
             "yes_bid_dollars": None, "yes_ask_dollars": None},             # no quote
        ]
        page = {"events": [{"markets": bad + [self._mk("GOOD")]}], "cursor": ""}
        feed, _ = self._feed_returning([page])
        markets = asyncio.run(feed.fetch_markets())
        assert [m.id for m in markets] == ["kalshi:GOOD"]

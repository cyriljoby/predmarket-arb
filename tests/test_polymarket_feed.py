"""Unit tests for Polymarket US market-data normalization."""

import asyncio
from datetime import UTC, datetime

from pmarb.feeds.polymarket import (
    PolymarketUSFeed,
    _line_event,
    _match_question,
    _team_name,
    normalize_market_data,
)
from pmarb.models import PriceLevel

OBSERVED = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
# Sentinel for "the venue stops sending but never closes the socket" — the
# failure that cost the 30-day run 15.1h in one stretch.
_SILENCE = object()
MARKET = {
    "slug": "tec-mlb-nlchamp",
    "question": "National League Champion",
    "endDate": "2026-09-27T00:00:00Z",
    "category": "sports",
}
# Real shape: bids = YES bids (desc), offers = YES asks (asc).
MARKET_DATA = {
    "bids": [
        {"px": {"value": "0.4090", "currency": "USD"}, "qty": "62.0"},
        {"px": {"value": "0.4000", "currency": "USD"}, "qty": "320.0"},
    ],
    "offers": [
        {"px": {"value": "0.4100", "currency": "USD"}, "qty": "1244.0"},
        {"px": {"value": "0.4200", "currency": "USD"}, "qty": "20202.0"},
    ],
}


class TestNormalization:
    def test_yes_depth_is_offers_directly(self):
        m = normalize_market_data(MARKET, MARKET_DATA, OBSERVED)
        assert m.yes_depth == (PriceLevel(0.41, 1244.0), PriceLevel(0.42, 20202.0))
        assert m.yes_ask == 0.41

    def test_no_depth_derived_from_yes_bids(self):
        m = normalize_market_data(MARKET, MARKET_DATA, OBSERVED)
        # YES bids 0.409/0.400 -> NO asks at (1 - p) = 0.591/0.600, cheapest first.
        assert m.no_depth == (PriceLevel(0.591, 62.0), PriceLevel(0.6, 320.0))
        assert m.no_ask == 0.591

    def test_reference_bids(self):
        m = normalize_market_data(MARKET, MARKET_DATA, OBSERVED)
        assert m.yes_bid == 0.409                 # best YES bid
        assert m.no_bid == 0.59                    # 1 - best YES offer (0.41)

    def test_identity_and_tz(self):
        m = normalize_market_data(MARKET, MARKET_DATA, OBSERVED)
        assert m.id == "polymarket_us:tec-mlb-nlchamp"
        assert m.platform == "polymarket_us"
        assert m.resolution_date.tzinfo is not None

    def test_depth_sorted_cheapest_first(self):
        m = normalize_market_data(MARKET, MARKET_DATA, OBSERVED)
        assert list(m.yes_depth) == sorted(m.yes_depth, key=lambda lvl: lvl.price)
        assert list(m.no_depth) == sorted(m.no_depth, key=lambda lvl: lvl.price)

    def test_empty_book(self):
        m = normalize_market_data(MARKET, {}, OBSERVED)
        assert m.yes_depth == ()
        assert m.no_depth == ()
        assert m.yes_ask is None and m.no_ask is None


class TestMatchQuestion:
    def test_futures_uses_description_entity(self):
        m = {
            "question": "FIFA World Cup Winner",
            "description": "Will Spain win the 2026 FIFA World Cup, scheduled to "
            "conclude July 19, 2026? If the event is postponed, delayed...",
        }
        assert _match_question(m) == "Will Spain win the 2026 FIFA World Cup"

    def test_strips_settle_to_yes_if_leadin(self):
        m = {
            "question": "Pro Football MVP",
            "description": "This market will settle to Yes if Tyler Shough wins the "
            "Pro Football AP MVP Award for the 2026-27 regular season. Outcome "
            "sourced from the relevant governing body.",
        }
        assert _match_question(m) == (
            "Tyler Shough wins the Pro Football AP MVP Award for the 2026-27 "
            "regular season."
        )

    def test_strips_scheduled_tail(self):
        m = {
            "question": "x",
            "description": "This market resolves to Yes if Misa Esports wins Map 2 "
            "vs Inner Circle Academy, scheduled for July 1, 2026 at 10:30 AM UTC. "
            "Otherwise No.",
        }
        assert _match_question(m) == "Misa Esports wins Map 2 vs Inner Circle Academy"

    def test_falls_back_to_question_without_description(self):
        assert _match_question({"question": "National League Champion"}) == (
            "National League Champion"
        )


class TestTradeableFilter:
    def test_accepts_binary_live_market(self):
        assert PolymarketUSFeed._is_tradeable(
            {"slug": "s", "endDate": "2026-09-01T00:00:00Z",
             "closed": False, "outcomes": '["Yes","No"]'}
        )

    def test_rejects_closed(self):
        assert not PolymarketUSFeed._is_tradeable(
            {"slug": "s", "endDate": "2026-09-01T00:00:00Z",
             "closed": True, "outcomes": '["Yes","No"]'}
        )

    def test_rejects_non_binary(self):
        assert not PolymarketUSFeed._is_tradeable(
            {"slug": "s", "endDate": "2026-09-01T00:00:00Z",
             "closed": False, "outcomes": '["A","B","C"]'}
        )

    def test_rejects_missing_slug_or_date(self):
        assert not PolymarketUSFeed._is_tradeable({"outcomes": '["Yes","No"]'})


class TestPagination:
    def _feed_returning(self, pages):
        feed = PolymarketUSFeed.__new__(PolymarketUSFeed)
        feed._PAGE = 2
        feed._MAX_PAGES = 100
        calls = []

        async def fake_get(path, params):
            calls.append(int(params["offset"]))
            return pages[len(calls) - 1]

        feed._get_gateway = fake_get
        return feed, calls

    @staticmethod
    def _mk(slug):
        return {"slug": slug, "endDate": "2026-09-01T00:00:00Z",
                "closed": False, "outcomes": '["Yes","No"]', "question": "q"}

    def test_walks_pages_until_short(self):
        pages = [
            {"markets": [self._mk("a"), self._mk("b")]},  # full page -> continue
            {"markets": [self._mk("c")]},                  # short page -> stop
        ]
        feed, calls = self._feed_returning(pages)
        markets = asyncio.run(feed.fetch_markets())
        assert [m.id for m in markets] == [
            "polymarket_us:a", "polymarket_us:b", "polymarket_us:c"
        ]
        assert calls == [0, 2]  # offsets advanced by PAGE


class TestStreamBooks:
    """Offline test of the WS streaming loop (mocked socket)."""

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
            except StopIteration:  # end the stream
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

    def test_yields_markets_and_batches_subscribe(self, monkeypatch):
        import json

        import websockets

        from pmarb.feeds import polymarket as pmod

        meta = {**MARKET, "slug": "slug-a"}
        market = pmod._market_metadata(meta, OBSERVED)
        messages = [
            json.dumps({"marketData": {"marketSlug": "slug-a", **MARKET_DATA}}),
            json.dumps({"marketData": {"marketSlug": "not-subscribed", **MARKET_DATA}}),
        ]
        ws = self._FakeWS(messages)
        monkeypatch.setattr(pmod.websockets, "connect",
                            lambda *a, **k: self._FakeConnect(ws))
        monkeypatch.setattr(pmod, "polymarket_us_headers", lambda *a, **k: {})

        feed = PolymarketUSFeed.__new__(PolymarketUSFeed)
        feed._creds = None

        async def collect():
            out = []
            try:
                async for m in feed.stream_books([market], reconnect=False):
                    out.append(m)
            except websockets.ConnectionClosed:
                pass
            return out

        out = asyncio.run(collect())
        # only the subscribed slug is yielded; unknown slug ignored
        assert len(out) == 1
        assert out[0].id == "polymarket_us:slug-a"
        assert out[0].yes_ask == 0.41
        # one subscribe message sent, carrying the slug
        assert len(ws.sent) == 1
        assert json.loads(ws.sent[0])["subscribe"]["marketSlugs"] == ["slug-a"]

    def test_every_streamed_book_carries_its_receipt_instant(self, monkeypatch):
        # Detection latency is measured from this stamp. A book that arrives
        # without one is invisible to the survival curve, and a stamp taken
        # after the parse would quietly exclude the parse from the measurement.
        import json
        import time

        import websockets

        from pmarb.feeds import polymarket as pmod

        meta = {**MARKET, "slug": "slug-a"}
        market = pmod._market_metadata(meta, OBSERVED)
        ws = self._FakeWS([
            json.dumps({"marketData": {"marketSlug": "slug-a", **MARKET_DATA}}),
        ])
        monkeypatch.setattr(pmod.websockets, "connect",
                            lambda *a, **k: self._FakeConnect(ws))
        monkeypatch.setattr(pmod, "polymarket_us_headers", lambda *a, **k: {})
        feed = PolymarketUSFeed.__new__(PolymarketUSFeed)
        feed._creds = None

        async def collect():
            out = []
            try:
                async for m in feed.stream_books([market], reconnect=False):
                    out.append(m)
            except websockets.ConnectionClosed:
                pass
            return out

        before = time.monotonic()
        out = asyncio.run(collect())
        after = time.monotonic()
        assert out[0].received_mono is not None
        assert before <= out[0].received_mono <= after

    def test_a_silent_socket_is_torn_down_and_resubscribed(self, monkeypatch):
        # The 30-day run's dominant failure: the host suspends, the socket goes
        # half-open, and on wake the read blocks on a connection nothing will
        # ever write to again. Nothing raises, so the reconnect handler never
        # fires. Only the ABSENCE of data reveals it.
        import json

        import websockets

        from pmarb.feeds import polymarket as pmod

        meta = {**MARKET, "slug": "slug-a"}
        market = pmod._market_metadata(meta, OBSERVED)
        ws = self._FakeWS([
            json.dumps({"marketData": {"marketSlug": "slug-a", **MARKET_DATA}}),
            _SILENCE,
        ])
        monkeypatch.setattr(pmod.websockets, "connect",
                            lambda *a, **k: self._FakeConnect(ws))
        monkeypatch.setattr(pmod, "polymarket_us_headers", lambda *a, **k: {})

        feed = PolymarketUSFeed.__new__(PolymarketUSFeed)
        feed._creds = None

        async def collect():
            out = []
            try:
                async for m in feed.stream_books([market], reconnect=False,
                                                 idle_timeout=0.05):
                    out.append(m)
            except websockets.ConnectionClosed:
                pass
            return out

        out = asyncio.run(collect())
        assert len(out) == 1                  # the message before the silence
        # and it reconnected rather than hanging: every slug resubscribed.
        assert len(ws.sent) == 2


class TestCompetitorNaming:
    """College football exposed a two-field naming split on Poly's side.

    `team.name` is the MASCOT there ("Bulldogs") while `safeName` is the SCHOOL
    ("The Citadel"). Kalshi names the school, so a mascot-only competitor shares
    no token with it and the game cannot align at all — 176 CFB games matched
    nothing, silently. Everywhere else one field contains the other and either
    alone works, so the join must NOT fire there.
    """

    def test_mascot_and_school_are_joined(self):
        assert _team_name({"name": "Bulldogs", "safeName": "The Citadel"}) == \
            "The Citadel Bulldogs"
        assert _team_name({"name": "49ers", "safeName": "Charlotte"}) == \
            "Charlotte 49ers"

    def test_containment_is_left_alone(self):
        # "Atlanta Braves"/"Braves" and "Detroit Lions"/"Detroit" already match
        # by subset; joining would only duplicate tokens.
        assert _team_name({"name": "Atlanta Braves", "safeName": "Braves"}) == \
            "Atlanta Braves"
        assert _team_name({"name": "Detroit Lions", "safeName": "Detroit"}) == \
            "Detroit Lions"

    def test_the_longer_side_wins_a_containment(self):
        assert _team_name({"name": "Braves", "safeName": "Atlanta Braves"}) == \
            "Atlanta Braves"

    def test_missing_or_identical_safe_name_is_a_no_op(self):
        assert _team_name({"name": "Solo"}) == "Solo"
        assert _team_name({"name": "Solo", "safeName": ""}) == "Solo"
        assert _team_name({"name": "Solo", "safeName": "solo"}) == "Solo"


def _poly_line(**over) -> dict:
    base = {
        "marketType": "spreads", "sportsMarketType": "football_team_full_game_spread",
        "slug": "asc-nfl-den-kc-2026-09-14-pos-5pt5", "line": 5.5,
        "gameStartTime": "2026-09-15T00:15:00Z",
        "marketSides": [
            {"description": "+5.50", "long": True,
             "team": {"name": "Denver Broncos", "safeName": "Denver"}},
            {"description": "-5.50", "long": False,
             "team": {"name": "Kansas City Chiefs", "safeName": "Kansas City"}},
        ],
    }
    base.update(over)
    return base


class TestLineExtraction:
    """Spreads carry team objects; totals carry only prose. Both must yield the
    same identity, and every sub-period slice must be refused."""

    def test_spread_reads_teams_line_and_sign(self):
        le = _line_event(_poly_line())
        assert le.kind == "spread" and le.line == 5.5 and le.league == "nfl"
        assert le.yes_team == "Denver Broncos"
        assert le.yes_favored is False        # long side is "+5.50"

    def test_the_sign_flips_yes_favored(self):
        le = _line_event(_poly_line(marketSides=[
            {"description": "-5.50", "long": True,
             "team": {"name": "Denver Broncos", "safeName": "Denver"}},
            {"description": "+5.50", "long": False,
             "team": {"name": "Kansas City Chiefs", "safeName": "Kansas City"}},
        ]))
        assert le.yes_favored is True

    def test_an_unsigned_spread_side_is_refused(self):
        # Without a sign the contract is unknown, and guessing it inverts a
        # hedge. Refuse rather than assume.
        assert _line_event(_poly_line(marketSides=[
            {"description": "Denver", "long": True,
             "team": {"name": "Denver Broncos"}},
            {"description": "KC", "long": False,
             "team": {"name": "Kansas City Chiefs"}},
        ])) is None

    def test_total_reads_teams_from_the_description(self):
        le = _line_event(_poly_line(
            marketType="totals", sportsMarketType="football_team_full_game_total",
            slug="tsc-nfl-ne-sea-2026-09-09-total-28pt5", line=28.5,
            description=("This market will settle to Yes if New England Patriots "
                         "and Seattle Seahawks combine for over 28.5 points."),
            marketSides=[{"description": "Over", "long": True},
                         {"description": "Under", "long": False}]))
        assert le.kind == "total" and le.line == 28.5
        assert le.competitors == ("New England Patriots", "Seattle Seahawks")

    def test_a_leading_article_is_not_part_of_the_name(self):
        le = _line_event(_poly_line(
            marketType="totals", sportsMarketType="baseball_team_full_game_total",
            slug="tsc-mlb-az-hou-2026-09-06-6pt5", line=6.5,
            description=("settles to Yes if the Arizona Diamondbacks and Houston "
                         "Astros combine for over 6.5 runs."),
            marketSides=[{"description": "Over", "long": True},
                         {"description": "Under", "long": False}]))
        assert le.competitors == ("Arizona Diamondbacks", "Houston Astros")

    def test_half_and_quarter_slices_are_refused(self):
        # A first-half total is not a full-game total. Pairing them is the same
        # error as matching "Stage 9" to the overall race.
        for t in ("football_team_first_half_spread", "football_team_second_half_total",
                  "football_game_third_quarter_total", "soccer_team_first_half_total"):
            assert _line_event(_poly_line(sportsMarketType=t)) is None

    def test_team_totals_are_refused(self):
        # "football_team_points_full_game_total" contains "full_game" but is one
        # team's points, a different contract with its own Kalshi series.
        assert _line_event(_poly_line(
            marketType="totals",
            sportsMarketType="football_team_points_full_game_total")) is None

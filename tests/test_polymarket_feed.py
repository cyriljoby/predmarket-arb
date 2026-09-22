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


class TestQuestionIsNotTruncatedAtAnAbbreviation:
    """A period is not reliably a sentence end: the subjects are full of them.

    "Arizona St. advances to..." truncated to "Arizona St.", and "A.J. Brown
    records 1+ touchdowns..." to "A.J." — 187 of 5,727 pairs. Matching was
    unaffected (futures read the raw question, props match on structured
    identity), but this is the text a human or an LLM reads to verify a pair,
    so a review set built on it would have been judging fragments.
    """

    def test_a_trailing_abbreviation_does_not_end_the_sentence(self):
        q = _match_question({"description":
            "This market will settle to Yes if Arizona St. advances to the "
            "2026-27 College Football Playoff. Further terms apply.",
            "question": "fallback"})
        assert q.startswith("Arizona St. advances")

    def test_initials_do_not_end_the_sentence(self):
        q = _match_question({"description":
            "This market will settle to Yes if A.J. Brown records 1+ touchdowns "
            "(excluding passing touchdowns) in the game. Overtime is included.",
            "question": "fallback"})
        assert "A.J. Brown records" in q
        # the qualifier is the whole point of reading the question
        assert "excluding passing touchdowns" in q

    def test_a_normal_first_sentence_is_still_just_the_first(self):
        q = _match_question({"description":
            "This market will settle to Yes if Spain wins the 2026 FIFA World "
            "Cup. Extra time counts.", "question": "fallback"})
        assert q == "Spain wins the 2026 FIFA World Cup."


class TestResync:
    """Mutating live subscriptions, against a fake socket.

    Verified live 2026-09-21: `{"unsubscribe": {"requestId": rid}}` — requestId
    and NOTHING else, any extra field is rejected with `invalid_message` — is
    honoured AND frees cap slots. There is no per-slug removal, so the removable
    unit is whatever one subscribe call covered, which is what makes a partly-dead
    batch the interesting case.
    """

    class _MutableWS:
        def __init__(self, *, ack=True):
            self.sent = []
            self.closed = False
            self._ack = ack
            self._q: asyncio.Queue = asyncio.Queue()
            self.live: dict[str, list] = {}   # requestId -> slugs, venue-side

        async def send(self, raw):
            import json
            msg = json.loads(raw)
            self.sent.append(msg)
            if "subscribe" in msg:
                sub = msg["subscribe"]
                self.live[sub["requestId"]] = list(sub["marketSlugs"])
                return
            rid = msg["unsubscribe"]["requestId"]
            if self._ack:
                self.live.pop(rid, None)
                await self._q.put({"requestId": rid, "unsubscribed": True})

        async def push_book(self, slug):
            await self._q.put({"marketData": {"marketSlug": slug, **MARKET_DATA}})

        async def push_error(self, text):
            await self._q.put({"error": text})

        async def recv(self):
            import json
            return json.dumps(await self._q.get())

        async def close(self):
            self.closed = True

    def _markets(self, slugs):
        from pmarb.feeds import polymarket as pmod
        return [pmod._market_metadata({**MARKET, "slug": s}, OBSERVED)
                for s in slugs]

    def _drive(self, monkeypatch, slugs, body, *, ack=True, batch=None):
        """Run one live shard in the background and hand `body` the feed."""
        from pmarb.feeds import polymarket as pmod

        ws = self._MutableWS(ack=ack)
        monkeypatch.setattr(pmod.websockets, "connect",
                            lambda *a, **k: TestStreamBooks._FakeConnect(ws))
        monkeypatch.setattr(pmod, "polymarket_us_headers", lambda *a, **k: {})
        feed = PolymarketUSFeed.__new__(PolymarketUSFeed)
        feed._creds = None
        feed._init_stream_state()
        if batch is not None:
            feed._SUB_BATCH = batch
        markets = self._markets(slugs)
        seen = []

        async def go():
            async def read():
                async for mk in feed.stream_books(markets, reconnect=False,
                                                  idle_timeout=5.0):
                    seen.append(mk.id)

            task = asyncio.create_task(read())
            await ws.push_book(slugs[0])
            for _ in range(20):
                await asyncio.sleep(0)
                if seen:
                    break
            out = await body(feed, ws, markets)
            task.cancel()
            return out

        return asyncio.run(asyncio.wait_for(go(), timeout=5.0)), ws, seen

    def test_a_fully_dead_batch_is_just_unsubscribed(self, monkeypatch):
        # One subscribe per slug here, so dropping one touches nothing else.
        async def body(feed, ws, markets):
            return await feed.resync([markets[0]], error_grace=0.0)

        out, ws, _ = self._drive(monkeypatch, ["a", "b"], body, batch=1)
        assert out["dropped"] == 1 and out["applied"] == "live"
        unsubs = [m for m in ws.sent if "unsubscribe" in m]
        assert len(unsubs) == 1
        assert list(ws.live.values()) == [["a"]]
        # requestId ALONE — extra fields are rejected by the strict unmarshal.
        assert set(unsubs[0]["unsubscribe"]) == {"requestId"}

    def test_a_partly_dead_batch_is_torn_down_and_its_survivors_resubscribed(
            self, monkeypatch):
        # The awkward case: removal granularity is a whole subscribe batch, so
        # the survivors pay a brief gap. Acceptable only because Poly sends full
        # snapshots — the next message is a complete book, and the staleness gate
        # discards the interval rather than trusting it.
        async def body(feed, ws, markets):
            return await feed.resync(markets[:2], error_grace=0.0)

        out, ws, _ = self._drive(monkeypatch, ["a", "b", "c"], body, batch=3)
        assert out["dropped"] == 1
        subs = [m["subscribe"] for m in ws.sent if "subscribe" in m]
        assert subs[0]["marketSlugs"] == ["a", "b", "c"]     # the original batch
        assert subs[1]["marketSlugs"] == ["a", "b"]          # survivors, re-sent
        assert subs[1]["requestId"] != subs[0]["requestId"]  # a NEW handle
        assert list(ws.live.values()) == [["a", "b"]]

    def test_added_slugs_go_onto_a_shard_with_headroom(self, monkeypatch):
        async def body(feed, ws, markets):
            new = self._markets(["c"])
            out = await feed.resync(markets + new, error_grace=0.0)
            return out, list(feed._shards[0].meta_by_slug)

        (out, meta), ws, _ = self._drive(monkeypatch, ["a", "b"], body, batch=2)
        assert out["added"] == 1 and out["new_shards"] == 0
        assert meta == ["a", "b", "c"]
        assert [m["subscribe"]["marketSlugs"] for m in ws.sent
                if "subscribe" in m] == [["a", "b"], ["c"]]

    def test_overflowing_the_cap_starts_a_new_shard(self, monkeypatch):
        # The venue counts subscriptions per CONNECTION (2,000 hard), so growth
        # past the cap is only possible by adding a connection.
        async def body(feed, ws, markets):
            feed._MAX_SUBS_PER_CONN = 2
            out = await feed.resync(markets + self._markets(["c"]),
                                    error_grace=0.0)
            return out, [list(sh.meta_by_slug) for sh in feed._shards]

        (out, shards), _, _ = self._drive(monkeypatch, ["a", "b"], body, batch=2)
        assert out["new_shards"] == 1
        assert shards == [["a", "b"], ["c"]]

    def test_an_unacknowledged_unsubscribe_recycles_the_shard(self, monkeypatch):
        # An unsubscribe that is not confirmed may have freed nothing, and a shard
        # that believes it has headroom it does not have is how the cap bug
        # started. So: reconnect this shard with the corrected list.
        async def body(feed, ws, markets):
            return await feed.resync([markets[0]], ack_timeout=0.05,
                                     error_grace=0.0)

        out, ws, _ = self._drive(monkeypatch, ["a", "b"], body, ack=False,
                                 batch=1)
        assert out["applied"] == "recycled" and out["recycled"] == 1
        assert ws.closed is True

    def test_an_error_frame_after_subscribing_recycles_the_shard(self, monkeypatch):
        # Poly acks an unsubscribe but says NOTHING on a successful subscribe, so
        # a refused one arrives only as a later error frame. Watching for it is
        # the only honest verification available.
        async def body(feed, ws, markets):
            new = self._markets(["c"])

            async def erupt():
                await ws.push_error("max subscriptions per connection reached")

            asyncio.get_running_loop().create_task(erupt())
            return await feed.resync(markets + new, error_grace=0.05)

        out, ws, _ = self._drive(monkeypatch, ["a", "b"], body, batch=2)
        assert out["applied"] == "recycled"
        assert ws.closed is True

    def test_a_dropped_slug_stops_being_yielded(self, monkeypatch):
        async def body(feed, ws, markets):
            await feed.resync([markets[0]], error_grace=0.0)
            await ws.push_book("b")
            for _ in range(10):
                await asyncio.sleep(0)
            return None

        _, _, seen = self._drive(monkeypatch, ["a", "b"], body, batch=1)
        assert seen == ["polymarket_us:a"]

    def test_resync_without_a_stream_defers_to_the_next_connect(self):
        feed = PolymarketUSFeed.__new__(PolymarketUSFeed)
        feed._creds = None
        feed._init_stream_state()
        out = asyncio.run(feed.resync(self._markets(["a"])))
        assert out["applied"] == "on_reconnect"

"""Unit tests for Polymarket US market-data normalization."""

import asyncio
from datetime import datetime, timezone

from pmarb.feeds.polymarket import (
    PolymarketUSFeed,
    _match_question,
    normalize_market_data,
)
from pmarb.models import PriceLevel

OBSERVED = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
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
            {"markets": [self._mk("a"), self._mk("b")]},  # full page (PAGE=2) -> continue
            {"markets": [self._mk("c")]},                  # short page -> stop
        ]
        feed, calls = self._feed_returning(pages)
        markets = asyncio.run(feed.fetch_markets())
        assert [m.id for m in markets] == [
            "polymarket_us:a", "polymarket_us:b", "polymarket_us:c"
        ]
        assert calls == [0, 2]  # offsets advanced by PAGE

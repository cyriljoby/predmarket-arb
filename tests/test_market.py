"""Unit tests for the Market data contract."""

from datetime import datetime, timedelta

import pytest

from pmarb.models import Market, PriceLevel


def make_market(**overrides) -> Market:
    base = dict(
        id="kalshi:TEST-1",
        platform="kalshi",
        question="Will it rain tomorrow?",
        resolution_date=datetime(2026, 9, 1),
        category="weather",
        yes_depth=(PriceLevel(0.42, 100.0), PriceLevel(0.45, 200.0)),
        no_depth=(PriceLevel(0.55, 150.0), PriceLevel(0.58, 300.0)),
        updated_at=datetime(2026, 6, 24, 12, 0, 0),
    )
    base.update(overrides)
    return Market(**base)


class TestPriceLevel:
    def test_named_and_indexed_access_agree(self):
        lvl = PriceLevel(0.42, 100.0)
        assert lvl.price == 0.42 == lvl[0]
        assert lvl.size == 100.0 == lvl[1]


class TestMarketAsks:
    def test_ask_is_top_of_depth(self):
        m = make_market()
        assert m.yes_ask == 0.42  # cheapest yes level
        assert m.no_ask == 0.55   # cheapest no level

    def test_empty_depth_yields_none(self):
        m = make_market(yes_depth=(), no_depth=())
        assert m.yes_ask is None
        assert m.no_ask is None


class TestMarketImmutability:
    def test_frozen(self):
        m = make_market()
        with pytest.raises(Exception):  # FrozenInstanceError
            m.question = "changed"  # type: ignore[misc]


class TestStaleness:
    def test_age_seconds(self):
        m = make_market(updated_at=datetime(2026, 6, 24, 12, 0, 0))
        now = datetime(2026, 6, 24, 12, 0, 3)
        assert m.age_seconds(now) == 3.0

    def test_age_uses_timedelta(self):
        m = make_market()
        now = m.updated_at + timedelta(milliseconds=500)
        assert m.age_seconds(now) == 0.5

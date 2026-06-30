"""Unit tests for the arbitrage detector (pure, no network)."""

from datetime import datetime, timedelta, timezone

import pytest

from pmarb.detection.spread import (
    compute_fill_price,
    detect_pair,
    max_fillable_size,
)
from pmarb.fees import kalshi_fee_per_share, poly_us_taker_fee_per_share
from pmarb.models import Market, PriceLevel

NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)


def lvls(*pairs):
    return tuple(PriceLevel(p, s) for p, s in pairs)


class TestComputeFillPrice:
    def test_single_level(self):
        assert compute_fill_price(lvls((0.40, 100)), 50) == 0.40

    def test_walks_into_second_level(self):
        # 50 @ 0.40 + 30 @ 0.45 = 33.5 over 80 = 0.41875
        assert compute_fill_price(lvls((0.40, 50), (0.45, 100)), 80) == pytest.approx(0.41875)

    def test_insufficient_liquidity_is_none(self):
        assert compute_fill_price(lvls((0.40, 50)), 200) is None

    def test_nonpositive_is_none(self):
        assert compute_fill_price(lvls((0.40, 50)), 0) is None


class TestMaxFillableSize:
    def test_viable_fills_to_liquidity(self):
        plan = max_fillable_size(
            lvls((0.40, 100)), lvls((0.55, 100)),
            kalshi_fee_per_share, poly_us_taker_fee_per_share, buffer=0.01, cap=1000,
        )
        assert plan.size == 100  # bounded by the thinner book
        # 1 - 0.40 - 0.55 - 0.0168 - 0.0124 = 0.0208
        assert plan.fee_adjusted_spread == pytest.approx(0.0208, abs=1e-4)

    def test_not_viable_returns_zero(self):
        # 0.42 + 0.55 leaves only ~0.0005 after fees — under the 0.01 buffer.
        plan = max_fillable_size(
            lvls((0.42, 100)), lvls((0.55, 100)),
            kalshi_fee_per_share, poly_us_taker_fee_per_share, buffer=0.01, cap=1000,
        )
        assert plan.size == 0

    def test_slippage_caps_size_below_deep_liquidity(self):
        # First 50 YES are cheap (0.40), then it jumps to 0.50 — slippage should
        # cap the fillable size well below the 1000 deep level.
        plan = max_fillable_size(
            lvls((0.40, 50), (0.50, 1000)), lvls((0.55, 2000)),
            kalshi_fee_per_share, poly_us_taker_fee_per_share, buffer=0.01, cap=1000,
        )
        assert 50 <= plan.size < 100

    def test_cap_limits_size(self):
        plan = max_fillable_size(
            lvls((0.30, 100000)), lvls((0.30, 100000)),
            kalshi_fee_per_share, poly_us_taker_fee_per_share, buffer=0.01, cap=10,
        )
        assert plan.size == 10

    def test_fee_accumulated_per_level_not_on_average(self):
        # Two YES levels straddling 0.50 where fee(avg) != avg(fee). Verify the
        # reported yes fee equals the per-level accumulation, not fee(avg_price).
        plan = max_fillable_size(
            lvls((0.30, 1), (0.70, 1)), lvls((0.10, 2)),
            kalshi_fee_per_share, poly_us_taker_fee_per_share, buffer=0.0, cap=1000,
        )
        assert plan.size == 2
        per_level = (kalshi_fee_per_share(0.30) + kalshi_fee_per_share(0.70)) / 2
        assert plan.yes_fee_per_share == pytest.approx(per_level)
        # fee on the average price (0.50) would be larger — confirm we did NOT use it
        assert plan.yes_fee_per_share < kalshi_fee_per_share(0.50)


def make_market(platform, mid, *, yes=(), no=(), at=NOW):
    return Market(
        id=f"{platform}:{mid}", platform=platform, question="q",
        resolution_date=NOW, category="", yes_depth=yes, no_depth=no, updated_at=at,
    )


class TestDetectPair:
    def test_detects_one_direction(self):
        # YES cheap on Kalshi, NO cheap on Poly US -> viable that direction only.
        k = make_market("kalshi", "K1", yes=lvls((0.40, 100)), no=())
        p = make_market("polymarket_us", "P1", yes=(), no=lvls((0.55, 100)))
        opps = detect_pair(k, p, NOW)
        assert len(opps) == 1
        o = opps[0]
        assert o.yes_platform == "kalshi" and o.no_platform == "polymarket_us"
        assert o.size == 100

    def test_no_arb_returns_empty(self):
        k = make_market("kalshi", "K1", yes=lvls((0.42, 100)), no=lvls((0.60, 100)))
        p = make_market("polymarket_us", "P1", yes=lvls((0.60, 100)), no=lvls((0.55, 100)))
        assert detect_pair(k, p, NOW) == []

    def test_staleness_gate_suppresses(self):
        # Poly leg is 10s old; default staleness tolerance is 2s -> skip entirely.
        k = make_market("kalshi", "K1", yes=lvls((0.40, 100)))
        p = make_market("polymarket_us", "P1", no=lvls((0.55, 100)), at=NOW - timedelta(seconds=10))
        assert detect_pair(k, p, NOW) == []

"""Unit tests for the per-venue fee functions."""

import pytest

from pmarb.fees import (
    kalshi_fee_per_share,
    poly_us_maker_rebate_per_share,
    poly_us_taker_fee_per_share,
    quadratic_fee,
)


class TestQuadraticFee:
    def test_peaks_at_fifty_cents(self):
        assert quadratic_fee(0.05, 0.50) == 0.0125  # 0.05 * 0.25

    def test_symmetric_around_fifty_cents(self):
        # p(1-p) is symmetric, so the fee at p equals the fee at 1-p.
        assert quadratic_fee(0.07, 0.42) == quadratic_fee(0.07, 0.58)

    def test_zero_at_boundaries(self):
        assert quadratic_fee(0.07, 0.0) == 0.0
        assert quadratic_fee(0.07, 1.0) == 0.0

    def test_negative_coefficient_is_a_credit(self):
        # A maker rebate (negative theta) yields a negative fee — a credit.
        assert quadratic_fee(-0.0125, 0.50) == pytest.approx(-0.003125, abs=1e-4)
        assert quadratic_fee(-0.0125, 0.50) < 0

    @pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
    def test_rejects_out_of_range(self, bad):
        with pytest.raises(ValueError):
            quadratic_fee(0.07, bad)


class TestKalshiFee:
    def test_peak(self):
        assert kalshi_fee_per_share(0.50) == 0.0175  # 0.07 * 0.25

    def test_rounded_to_four_places(self):
        # 0.07 * 0.42 * 0.58 = 0.017052 -> 0.0171
        assert kalshi_fee_per_share(0.42) == 0.0171


class TestPolyUSFee:
    def test_taker_peak(self):
        assert poly_us_taker_fee_per_share(0.50) == 0.0125

    def test_taker_offpeak_is_below_peak(self):
        # 0.05 * 0.42 * 0.58 = 0.01218 -> 0.0122, below the $0.0125 peak.
        assert poly_us_taker_fee_per_share(0.42) == 0.0122
        assert poly_us_taker_fee_per_share(0.42) < poly_us_taker_fee_per_share(0.50)

    def test_maker_rebate_is_negative(self):
        assert poly_us_maker_rebate_per_share(0.50) < 0

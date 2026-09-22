"""Window survival: was the edge still there Δ after it opened?

The trap this guards is censoring. When a pair's samples run out before t+Δ —
the run ended, the market delisted, the database was down — what happened is
UNKNOWN. Counting those as "closed" manufactures a decay curve that is really
the edge of the dataset, and it would decay fastest exactly where coverage is
worst, which is the most convincing possible wrong answer.
"""

from datetime import UTC, datetime, timedelta

from pmarb.backtest.survival import latency_summary, survival_curve
from pmarb.models import Sample

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
PAIR = ("kalshi:A", "polymarket_us:B")


def _s(offset_s: float, *, viable: bool, pair=PAIR, latency=None, partner=None):
    return Sample(
        observed_at=T0 + timedelta(seconds=offset_s),
        market_a_id=pair[0], market_b_id=pair[1],
        spread_top=0.03, spread_depth=0.02,
        spread_fee_adj=0.002 if viable else -0.001,
        fillable_size=50 if viable else 0,
        detect_latency_ms=latency, partner_age_ms=partner,
    )


def _at(points, delta_ms):
    return next(p for p in points if p.delta_ms == delta_ms)


class TestSurvival:
    def test_a_window_open_past_the_delta_counts_as_survived(self):
        rows = [_s(0, viable=True), _s(2, viable=True), _s(10, viable=False)]
        p = _at(survival_curve(rows, [1_000]), 1_000)
        assert (p.opened, p.survived, p.censored) == (1, 1, 0)
        assert p.pct == 100.0

    def test_a_window_closed_before_the_delta_counts_as_gone(self):
        rows = [_s(0, viable=True), _s(1, viable=False), _s(9, viable=False)]
        p = _at(survival_curve(rows, [5_000]), 5_000)
        assert (p.opened, p.survived) == (1, 0)
        assert p.pct == 0.0

    def test_state_persists_between_samples(self):
        # Books update irregularly; viability holds until a row says otherwise,
        # so a gap with no rows is not evidence the window shut.
        rows = [_s(0, viable=True), _s(30, viable=True)]
        assert _at(survival_curve(rows, [5_000]), 5_000).survived == 1

    def test_a_series_ending_before_the_delta_is_censored_not_closed(self):
        # THE failure this file exists for. Nothing observed after t+Δ means the
        # fate is unknown; calling it closed invents decay out of coverage.
        rows = [_s(0, viable=True), _s(1, viable=True)]
        p = _at(survival_curve(rows, [60_000]), 60_000)
        assert (p.opened, p.survived, p.censored) == (0, 0, 1)
        assert p.pct is None          # nothing known -> no rate, not 0%

    def test_censored_windows_are_excluded_from_the_rate(self):
        # One window observed shut before the target (known), one still open
        # when its data ran out (unknown).
        shut = [_s(0, viable=True), _s(1, viable=False), _s(9, viable=False)]
        cut = [_s(0, viable=True, pair=("kalshi:C", "polymarket_us:D")),
               _s(0.5, viable=True, pair=("kalshi:C", "polymarket_us:D"))]
        p = _at(survival_curve(shut + cut, [5_000]), 5_000)
        assert p.opened == 1 and p.censored == 1
        assert p.pct == 0.0           # the one KNOWN window had closed

    def test_each_reopening_is_its_own_window(self):
        rows = [_s(0, viable=True), _s(1, viable=False),
                _s(2, viable=True), _s(20, viable=True)]
        p = _at(survival_curve(rows, [0]), 0)
        assert p.opened == 2

    def test_delta_zero_is_every_window_by_definition(self):
        rows = [_s(0, viable=True), _s(5, viable=False)]
        p = _at(survival_curve(rows, [0]), 0)
        assert p.opened == 1 and p.survived == 1 and p.pct == 100.0

    def test_pairs_do_not_bleed_into_each_other(self):
        # One pair shut at 1s, the other still open at 30s. Neither is censored:
        # the first is observed shut, the second observed open past the target.
        other = ("kalshi:X", "polymarket_us:Y")
        rows = [_s(0, viable=True), _s(1, viable=False),
                _s(0, viable=True, pair=other), _s(30, viable=True, pair=other)]
        p = _at(survival_curve(rows, [5_000]), 5_000)
        assert (p.opened, p.survived, p.censored) == (2, 1, 0)

    def test_a_window_shut_before_the_data_ran_out_is_known_not_censored(self):
        # Observed shut at 1s, series ends at 1s, target is 5s. Reopening would
        # require an evaluation and none happened, so this is a known closure.
        rows = [_s(0, viable=True), _s(1, viable=False)]
        p = _at(survival_curve(rows, [5_000]), 5_000)
        assert (p.opened, p.survived, p.censored) == (1, 0, 0)


class TestLatencySummary:
    def test_reports_both_clocks_separately(self):
        rows = [_s(i, viable=True, latency=0.2 + i, partner=100 * (i + 1))
                for i in range(10)]
        out = latency_summary(rows)
        assert out["rows_with_latency"] == 10
        assert out["detect_ms"]["p50"] is not None
        assert out["partner_age_ms"]["p50"] >= out["detect_ms"]["p50"]

    def test_rows_without_latency_are_counted_not_defaulted(self):
        # A file-sourced row has no latency. Treating that as zero would report
        # instantaneous detection for the entire Phase 1 archive.
        rows = [_s(0, viable=True), _s(1, viable=True, latency=5.0, partner=200)]
        out = latency_summary(rows)
        assert out["rows_with_latency"] == 1
        assert out["rows_without_latency"] == 1

    def test_no_latency_anywhere_reports_none_not_zero(self):
        out = latency_summary([_s(0, viable=True)])
        assert out["detect_ms"]["p50"] is None
        assert out["partner_age_ms"]["p50"] is None

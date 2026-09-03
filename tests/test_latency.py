"""The latency histogram is a measuring instrument, so its error bound is the
thing under test.

Percentiles are reported as bucket ceilings: a claim of "p99 = 4ms" must mean
the true p99 is at most 4ms, never that it is roughly 4ms. Every assertion here
is one-sided in that direction.
"""

import math

import pytest

from pmarb.latency import _GROWTH, LatencyHistogram


class TestPercentiles:
    def test_empty_reports_nothing_rather_than_zero(self):
        # A run with no evaluations has no latency; zero would read as instant.
        h = LatencyHistogram()
        assert h.percentile(0.5) is None
        assert h.summary() == "lat n/a"

    def test_percentile_is_a_ceiling_within_one_bucket(self):
        h = LatencyHistogram()
        for _ in range(1000):
            h.observe(2.0)
        p50 = h.percentile(0.5)
        assert 2.0 <= p50 <= 2.0 * _GROWTH   # never under-reports, never by much

    def test_tail_is_not_swallowed_by_the_bulk(self):
        h = LatencyHistogram()
        for _ in range(990):
            h.observe(1.0)
        for _ in range(10):
            h.observe(500.0)                 # 1% of evaluations, 500x slower
        assert h.percentile(0.50) <= 1.0 * _GROWTH
        assert h.percentile(0.999) >= 500.0
        assert h.max_ms == 500.0

    @pytest.mark.parametrize("q", [0.5, 0.9, 0.99])
    def test_matches_an_exact_percentile_over_a_wide_spread(self, q):
        values = [0.05 * 1.5 ** i for i in range(40)]   # 0.05ms .. ~2 minutes
        h = LatencyHistogram()
        for v in values:
            h.observe(v)
        # nearest-rank: the smallest value at or above which q of the data sits
        exact = sorted(values)[math.ceil(q * len(values)) - 1]
        got = h.percentile(q)
        assert exact <= got <= exact * _GROWTH

    def test_percentile_never_exceeds_the_observed_maximum(self):
        # The last bucket is an overflow bucket whose edge is far above the
        # values in it; reporting that edge would invent latency.
        h = LatencyHistogram()
        h.observe(9.0)
        assert h.percentile(1.0) == 9.0


class TestObserveIsTotal:
    def test_a_zero_reading_lands_in_the_first_bucket(self):
        h = LatencyHistogram()
        h.observe(0.0)
        assert h.count == 1
        assert h.percentile(0.5) == 0.0      # clamped to the observed max

    def test_absurdly_large_readings_are_absorbed_not_lost(self):
        # A GC pause or a swapped-out process can produce a reading past the
        # last bucket edge. It must still be counted — that is exactly the
        # observation worth keeping.
        h = LatencyHistogram()
        h.observe(10_000_000.0)
        assert h.count == 1
        assert h.max_ms == 10_000_000.0
        assert h.percentile(0.99) == 10_000_000.0

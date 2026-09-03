"""Detection latency — how long this stack takes to see an edge.

Phase 1 measured how long windows LAST (86% a single sample, ~1.2s median).
That only becomes a decision if it is compared against how long this system
takes to NOTICE, and that number was never recorded. Without it "the arb is not
capturable at retail latency" rests on an unmeasured half.

So every evaluation is timed from the moment the venue's bytes arrive on the
socket to the moment the pair's spread is known. That is the honest Δ: it
covers only what this process controls (parse, book maintenance, normalize,
depth walk) and assumes nothing about fills, queue position, or venue-side
delay, none of which this project can observe.

Buckets, not samples. A multi-week run evaluates on the order of 10^8 times and
the live report wants percentiles every ten seconds, so the distribution is kept
as fixed geometric buckets: O(1) memory, O(1) observe, and percentiles that are
exact to the bucket width (10%) rather than exact-but-unaffordable.
"""

from __future__ import annotations

import math

_MIN_MS = 0.01      # floor; anything faster lands in bucket 0
_GROWTH = 1.1       # bucket width, so a percentile is accurate to 10%
_BUCKETS = 180      # 0.01ms .. ~280s, the last bucket absorbing the tail

_LOG_GROWTH = math.log(_GROWTH)


class LatencyHistogram:
    """Geometric-bucket histogram over millisecond durations."""

    __slots__ = ("_counts", "count", "max_ms")

    def __init__(self) -> None:
        self._counts = [0] * _BUCKETS
        self.count = 0
        self.max_ms = 0.0

    def observe(self, ms: float) -> None:
        """Record one duration. Negative readings are impossible on a monotonic
        clock, but a clamp is cheaper than a corrupted percentile."""
        ms = max(ms, 0.0)
        self._counts[self._index(ms)] += 1
        self.count += 1
        if ms > self.max_ms:
            self.max_ms = ms

    def percentile(self, q: float) -> float | None:
        """The q-quantile (0..1), rounded UP to its bucket edge, or None if
        nothing has been observed. Rounding up keeps the reported latency a
        ceiling — the direction that cannot flatter the result."""
        if self.count == 0:
            return None
        target = q * self.count
        cumulative = 0
        for i, n in enumerate(self._counts):
            cumulative += n
            if cumulative >= target:
                # The last bucket is unbounded above, so its edge is NOT a
                # ceiling for what landed in it — the observed maximum is. And
                # clamping every bucket to max_ms keeps a sparse histogram from
                # reporting a latency larger than anything ever measured.
                if i == _BUCKETS - 1:
                    return self.max_ms
                return min(self._edge(i), self.max_ms)
        return self.max_ms

    def summary(self) -> str:
        """One-line p50/p99/max, for the collector's periodic report."""
        if self.count == 0:
            return "lat n/a"
        return (f"lat p50={self.percentile(0.50):.1f}ms "
                f"p99={self.percentile(0.99):.1f}ms "
                f"max={self.max_ms:.0f}ms")

    # -- bucket geometry ---------------------------------------------------- #
    @staticmethod
    def _index(ms: float) -> int:
        if ms <= _MIN_MS:
            return 0
        i = math.ceil(math.log(ms / _MIN_MS) / _LOG_GROWTH)
        return min(i, _BUCKETS - 1)

    @staticmethod
    def _edge(i: int) -> float:
        """Upper bound of bucket `i`."""
        return _MIN_MS * _GROWTH ** i

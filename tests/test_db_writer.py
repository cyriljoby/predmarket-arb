"""Writer behaviour that does NOT need a database.

The hot-path guarantees are queue mechanics, so they are tested directly. Rows
reaching Postgres correctly is covered by tests/test_db_integration.py, which
skips when no database is reachable.
"""

import asyncio

from pmarb.db.writer import ObservationWriter, to_row
from pmarb.detection.spread import PairEvaluation

EV = PairEvaluation(
    yes_platform="kalshi", no_platform="polymarket_us",
    yes_ask_top=0.42, no_ask_top=0.55, estimated_fillable_size=47,
    yes_fill_price=0.431, no_fill_price=0.558,
    raw_spread_top_of_book=0.03, raw_spread_depth_adjusted=0.011,
    yes_fee_per_share=0.0172, no_fee_per_share=0.0100,
    fee_adjusted_spread=0.0028,
    frontier=((12, 0.0091), (24, 0.0060), (36, 0.0041)),
)


class TestSubmitNeverBlocks:
    """A stalled read means a sequence gap means a full resnapshot of ~3,000
    books. Dropping observations is recoverable; stalling the feed is not."""

    def test_full_queue_drops_instead_of_blocking(self):
        async def go():
            w = ObservationWriter("postgresql://unused", maxsize=3)
            for _ in range(10):
                w.submit(("row",))          # writer task never started
            return w.stats

        stats = asyncio.run(asyncio.wait_for(go(), timeout=2.0))
        assert stats.queued == 3
        assert stats.dropped == 7           # counted, not silently discarded

    def test_submit_is_synchronous(self):
        # submit() must be callable from the stream loop without awaiting.
        w = ObservationWriter("postgresql://unused", maxsize=10)
        w.submit(("row",))
        assert w.stats.queued == 1


class TestRowMapping:
    def test_column_order_matches_the_insert(self):
        row = to_row(EV, match_pair_id=7, reason=2,
                     observed_at="T", resolution_a="A", resolution_b="B")
        assert row[0] == 7                  # match_pair_id
        assert row[1] == "T"                # observed_at
        assert row[2] == 2                  # sample_reason
        assert row[3] == "kalshi"           # yes_venue
        assert row[13] == 47                # fillable_size
        assert row[-2:] == ("A", "B")       # both resolution dates

    def test_frontier_lands_in_its_six_columns(self):
        row = to_row(EV, 1, 0, "T", None, None)
        assert row[14:20] == (12, 0.0091, 24, 0.0060, 36, 0.0041)

    def test_missing_frontier_is_null_not_zero(self):
        # A zero edge at p25 would read as "measured and flat" rather than
        # "never cleared, so there was no walk to sample".
        from dataclasses import replace
        row = to_row(replace(EV, estimated_fillable_size=0, frontier=()),
                     1, 0, "T", None, None)
        assert row[14:20] == (None,) * 6

    def test_all_three_spreads_are_carried(self):
        row = to_row(EV, 1, 0, "T", None, None)
        assert row[10] == 0.03              # top of book
        assert row[11] == 0.011             # after slippage
        assert row[12] == 0.0028            # after fees

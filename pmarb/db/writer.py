"""Async batched writer for observations.

THE HARD CONSTRAINT: this must never block the WebSocket reader. A stalled read
means the feed falls behind, which means a sequence gap, which means Kalshi tears
down and resnapshots ~3,000 books. So the hot path only ever does a non-blocking
put onto a bounded queue; all database work happens on a separate task.

When the queue is full — database down, or a burst outrunning the writer — rows
are DROPPED and counted, never awaited. Losing observations is recoverable;
losing the book stream is not. The drop counter is the signal that the database
could not keep up, and it must be surfaced rather than swallowed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

import psycopg

_INSERT = """
    INSERT INTO observation (
        match_pair_id, observed_at, sample_reason, yes_venue,
        yes_ask_top, no_ask_top, yes_fill_price, no_fill_price,
        yes_fee, no_fee, spread_top, spread_depth, spread_fee_adj,
        fillable_size,
        size_at_p25, edge_at_p25, size_at_p50, edge_at_p50,
        size_at_p75, edge_at_p75,
        resolution_date_a, resolution_date_b)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (match_pair_id, observed_at) DO NOTHING
"""


@dataclass(slots=True)
class WriterStats:
    queued: int = 0
    written: int = 0
    dropped: int = 0     # queue full — the database could not keep up
    failed: int = 0      # batches lost to a database error
    batches: int = 0


class ObservationWriter:
    """Bounded queue in front of batched INSERTs on a background task."""

    def __init__(self, dsn: str, *, maxsize: int = 20_000,
                 batch_size: int = 500, flush_seconds: float = 2.0):
        self._dsn = dsn
        self._q: asyncio.Queue[tuple] = asyncio.Queue(maxsize=maxsize)
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._task: asyncio.Task | None = None
        self.stats = WriterStats()

    # -- hot path ---------------------------------------------------------- #
    def submit(self, row: tuple) -> None:
        """Non-blocking. Drops on a full queue rather than applying backpressure."""
        try:
            self._q.put_nowait(row)
            self.stats.queued += 1
        except asyncio.QueueFull:
            self.stats.dropped += 1

    # -- lifecycle --------------------------------------------------------- #
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Drain what is queued, then shut down."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(self._drain_remaining)

    def _drain_remaining(self) -> None:
        rows = []
        while not self._q.empty():
            rows.append(self._q.get_nowait())
        if rows:
            self._write(rows)

    # -- background -------------------------------------------------------- #
    async def _run(self) -> None:
        batch: list[tuple] = []
        while True:
            try:
                row = await asyncio.wait_for(self._q.get(),
                                             timeout=self._flush_seconds)
                batch.append(row)
            except TimeoutError:
                pass                      # idle flush keeps latency bounded
            if batch and (len(batch) >= self._batch_size
                          or self._q.empty()):
                to_write, batch = batch, []
                # to_thread: psycopg's sync driver would otherwise block the
                # event loop, which is the exact thing this class exists to avoid.
                await asyncio.to_thread(self._write, to_write)

    def _write(self, rows: list[tuple]) -> None:
        try:
            with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
                cur.executemany(_INSERT, rows)
                conn.commit()
            self.stats.written += len(rows)
            self.stats.batches += 1
        except Exception as exc:
            # A database problem must not kill collection. Count it loudly and
            # carry on; the rows are gone, the stream is not.
            self.stats.failed += len(rows)
            print(f"  WARNING observation write failed ({len(rows)} rows): "
                  f"{type(exc).__name__}: {exc}")


def _frontier_cells(frontier) -> tuple:
    """Flatten the size/edge frontier into its six columns.

    NULLs rather than zeros when the walk never cleared: no size was viable, so
    there is no edge at any quarter-point, and a zero would read as one.
    """
    if not frontier:
        return (None,) * 6
    return tuple(cell for point in frontier for cell in point)


def to_row(ev, match_pair_id: int, reason: int, observed_at: datetime,
           resolution_a: datetime | None, resolution_b: datetime | None) -> tuple:
    """Map a PairEvaluation onto the observation column order."""
    return (
        match_pair_id, observed_at, reason, ev.yes_platform,
        ev.yes_ask_top, ev.no_ask_top, ev.yes_fill_price, ev.no_fill_price,
        ev.yes_fee_per_share, ev.no_fee_per_share,
        ev.raw_spread_top_of_book, ev.raw_spread_depth_adjusted,
        ev.fee_adjusted_spread, ev.estimated_fillable_size,
        *_frontier_cells(ev.frontier),
        resolution_a, resolution_b,
    )

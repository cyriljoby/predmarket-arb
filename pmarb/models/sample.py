"""The backtest's input contract.

One observation of one matched pair, in the SCHEMA's vocabulary rather than the
Phase 1 log's. That direction matters: the database is deliberately
venue-generic (`market_a` / `market_b`) so a third venue needs no migration, and
translating back into Kalshi/Poly-specific names would re-couple the analysis to
two venues.

Both sources — `backtest.load_from_files` for JSONL, `db.sources.load_from_db`
for Postgres — construct these, so the funnel has one vocabulary and a renamed
column fails at the boundary instead of surfacing as a None deep inside a
percentage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Why a row exists, mirroring pmarb.main. `None` means the source did not record
# a reason (every Phase 1 JSONL row, which predates the field).
HEARTBEAT, EDGE_CHANGE, VIABLE, WINDOW_CLOSE = 0, 1, 2, 3


@dataclass(frozen=True, slots=True)
class Sample:
    """One evaluation of one pair at one instant.

    Leg fields are YES/NO — the two sides of the hedge — not venue-named;
    `yes_venue` says which venue is carrying the YES leg. That keeps the record
    generic while still recording direction.
    """

    observed_at: datetime
    market_a_id: str
    market_b_id: str
    spread_top: float          # before slippage or fees
    spread_depth: float        # after slippage, before fees
    spread_fee_adj: float      # after both — the only viability gate
    fillable_size: int         # 0 = nothing clears the gate

    question: str = ""
    match_method: str = ""
    yes_venue: str = ""
    sample_reason: int | None = None

    ask_top_yes: float | None = None
    ask_top_no: float | None = None
    fill_yes: float | None = None
    fill_no: float | None = None
    fee_yes: float | None = None
    fee_no: float | None = None

    # Captured per observation, never joined from `market`, because venues amend
    # settlement dates (a postponed game is rewritten) and a later join would
    # silently restate every historical holding period.
    resolution_date_a: datetime | None = None
    resolution_date_b: datetime | None = None

    @property
    def pair(self) -> tuple[str, str]:
        return (self.market_a_id, self.market_b_id)

    @property
    def viable(self) -> bool:
        """The single post-everything gate: positive edge AND fillable size."""
        return self.spread_fee_adj > 0 and self.fillable_size > 0

    @property
    def total_fee(self) -> float:
        return (self.fee_yes or 0.0) + (self.fee_no or 0.0)

    @property
    def settles_at(self) -> datetime | None:
        """When capital is freed — the LATER leg, since both must settle."""
        dates = [d for d in (self.resolution_date_a, self.resolution_date_b) if d]
        return max(dates) if dates else None

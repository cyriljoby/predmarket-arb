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
#
# UNSUBSCRIBED is a close marker written by the daily match refresh when a pair
# is dropped while its window is still open (the game settled, the market
# delisted). It is deliberately NOT WINDOW_CLOSE: that reason means "an
# evaluation found the edge gone", which is an observation, whereas this one
# means "we stopped looking", which is censoring. Conflating them would let the
# survival curve read our own unsubscribe as the market closing the window.
HEARTBEAT, EDGE_CHANGE, VIABLE, WINDOW_CLOSE, UNSUBSCRIBED = 0, 1, 2, 3, 4


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

    # How long this stack took to see this edge, and how stale the other leg
    # was when it did. None for any source that predates the instrumentation
    # (every Phase 1 JSONL row) — which is why the survival curve reports its
    # own coverage rather than assuming zero.
    detect_latency_ms: float | None = None
    partner_age_ms: int | None = None

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

    @property
    def days_to_settlement(self) -> int | None:
        if self.settles_at is None:
            return None
        return max((self.settles_at - self.observed_at).days, 0)

    @property
    def cost_per_share(self) -> float | None:
        """What one hedged share costs: both legs, at depth-walked fill prices."""
        if self.fill_yes is None or self.fill_no is None:
            return None
        return self.fill_yes + self.fill_no

    @property
    def annualised_return(self) -> float | None:
        """Return per year on the capital this hedge locks up, as a fraction.

        `spread_fee_adj` is time-blind: it ranks 1c on a three-year presidential
        contract above 0.9c on a game settling tonight, when the second is worth
        roughly 40x more. Capital is committed until BOTH legs settle, so the
        holding period is what converts an edge into a rate.

        Simple (not compounded) and 365-day, because the input is a point
        estimate off a sub-second window — compounding it would imply a
        precision the measurement does not have. Sub-day horizons floor at one
        day for the same reason.
        """
        days, cost = self.days_to_settlement, self.cost_per_share
        if days is None or not cost:
            return None
        return (self.spread_fee_adj / cost) * 365.0 / max(days, 1)

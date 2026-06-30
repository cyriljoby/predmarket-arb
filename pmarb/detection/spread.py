"""Fee- and slippage-aware arbitrage detector — the pure correctness core.

Given a matched pair of Markets (one per venue), decide whether buying YES on one
and NO on the other clears $1.00 after fees, and how many contracts stay viable
once depth/slippage is walked. No network, no state — pure functions on Markets.

Two operations (see CLAUDE.md):
  * compute_fill_price(depth, shares) — fixed size in, average price out. Models
    slippage for a chosen size. Used by Phase 2 execution.
  * max_fillable_size(...)            — the detection workhorse: finds the LARGEST
    size that still clears the gate, accumulating fees per depth level.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from pmarb.config import (
    MAX_FILLABLE_CAP,
    MAX_LEG_STALENESS_SECONDS,
    SLIPPAGE_BUFFER,
)
from pmarb.fees import kalshi_fee_per_share, poly_us_taker_fee_per_share
from pmarb.models import Market, PriceLevel

# A per-share taker fee as a function of fill price, looked up by platform.
FeeFn = Callable[[float], float]
_TAKER_FEE: dict[str, FeeFn] = {
    "kalshi": kalshi_fee_per_share,
    "polymarket_us": poly_us_taker_fee_per_share,
}


@dataclass(frozen=True, slots=True)
class FillPlan:
    """The economics of the largest viable hedge on a pair, in one direction."""

    size: int                   # contracts fillable on BOTH legs while viable (0 = none)
    avg_yes_fill: float         # depth-walked average price paid for YES
    avg_no_fill: float          # depth-walked average price paid for NO
    yes_fee_per_share: float    # level-accumulated YES fee / size
    no_fee_per_share: float     # level-accumulated NO fee / size
    fee_adjusted_spread: float  # per-share profit after slippage + fees


@dataclass(frozen=True, slots=True)
class Opportunity:
    """A detected arb in a specific direction (which venue is the YES leg)."""

    yes_platform: str
    no_platform: str
    yes_market_id: str
    no_market_id: str
    size: int
    avg_yes_fill: float
    avg_no_fill: float
    yes_fee_per_share: float
    no_fee_per_share: float
    fee_adjusted_spread: float


def compute_fill_price(depth: tuple[PriceLevel, ...], shares: int) -> float | None:
    """Average price to buy exactly `shares`, walking depth. None if the book
    lacks the liquidity to fill it. All-or-nothing (Phase 2 execution)."""
    if shares <= 0:
        return None
    remaining = float(shares)
    cost = 0.0
    for price, size in depth:
        take = min(remaining, size)
        cost += take * price
        remaining -= take
        if remaining <= 0:
            return cost / shares
    return None  # insufficient liquidity


def _whole_shares(depth: tuple[PriceLevel, ...]):
    """Yield one (whole) share's price at a time, walking the ladder."""
    for price, size in depth:
        for _ in range(int(size)):
            yield price


def max_fillable_size(
    yes_depth: tuple[PriceLevel, ...],
    no_depth: tuple[PriceLevel, ...],
    yes_fee_fn: FeeFn,
    no_fee_fn: FeeFn,
    buffer: float,
    cap: int,
) -> FillPlan:
    """Largest hedge size whose *average* per-share economics still clear the gate.

    Walks both ladders in lockstep (equal shares per leg — a hedge needs 1 YES +
    1 NO). Accumulates each leg's fee PER LEVEL on the fill price (the fee curve is
    concave, so fee(avg) overstates it). Records the largest size that clears, so
    the result is correct even if the gate isn't strictly monotonic.

    Early exit: fees are non-negative, so once `avg_yes + avg_no >= 1 - buffer`
    no larger size can ever clear (average fills only rise) — we stop.
    """
    yes_gen, no_gen = _whole_shares(yes_depth), _whole_shares(no_depth)
    filled = 0
    yes_cost = no_cost = yes_fee = no_fee = 0.0
    best = FillPlan(0, 0.0, 0.0, 0.0, 0.0, 0.0)

    while filled < cap:
        yp = next(yes_gen, None)
        np_ = next(no_gen, None)
        if yp is None or np_ is None:
            break  # a leg ran out of liquidity — hedge is bounded by the thinner book

        filled += 1
        yes_cost += yp
        no_cost += np_
        yes_fee += yes_fee_fn(yp)
        no_fee += no_fee_fn(np_)

        avg_yes = yes_cost / filled
        avg_no = no_cost / filled
        if avg_yes + avg_no >= 1.0 - buffer:
            break  # cannot be viable now or for any larger size

        spread = 1.0 - avg_yes - avg_no - yes_fee / filled - no_fee / filled
        if spread > buffer:
            best = FillPlan(
                filled, avg_yes, avg_no, yes_fee / filled, no_fee / filled, spread
            )
    return best


def detect_pair(
    a: Market,
    b: Market,
    now: datetime,
    *,
    buffer: float = SLIPPAGE_BUFFER,
    cap: int = MAX_FILLABLE_CAP,
    max_staleness: float = MAX_LEG_STALENESS_SECONDS,
) -> list[Opportunity]:
    """Detect arb on a matched pair, both directions, with a staleness gate.

    Returns an Opportunity for each viable direction (usually 0 or 1). Skips
    entirely if either leg's snapshot is staler than `max_staleness` — a fresh
    book compared against a stale one is a phantom, not an arb.
    """
    if a.age_seconds(now) > max_staleness or b.age_seconds(now) > max_staleness:
        return []

    opportunities: list[Opportunity] = []
    for yes_m, no_m in ((a, b), (b, a)):
        plan = max_fillable_size(
            yes_m.yes_depth,
            no_m.no_depth,
            _TAKER_FEE[yes_m.platform],
            _TAKER_FEE[no_m.platform],
            buffer,
            cap,
        )
        if plan.size > 0:
            opportunities.append(
                Opportunity(
                    yes_platform=yes_m.platform,
                    no_platform=no_m.platform,
                    yes_market_id=yes_m.id,
                    no_market_id=no_m.id,
                    size=plan.size,
                    avg_yes_fill=plan.avg_yes_fill,
                    avg_no_fill=plan.avg_no_fill,
                    yes_fee_per_share=plan.yes_fee_per_share,
                    no_fee_per_share=plan.no_fee_per_share,
                    fee_adjusted_spread=plan.fee_adjusted_spread,
                )
            )
    return opportunities

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


@dataclass(frozen=True, slots=True)
class PairEvaluation:
    """Full funnel record for a matched pair's best direction — for the LOGGER.

    Unlike `detect_pair` (viable directions only), this is produced for any pair
    with a positive top-of-book edge, so the backtest can see the honest
    attrition: apparent (top-of-book) -> post-slippage -> post-fees, where the
    later two may go negative. All three spread fields are always present.
    """

    yes_platform: str
    no_platform: str
    yes_ask_top: float
    no_ask_top: float
    estimated_fillable_size: int    # fee-viable size (0 = none clears the gate)
    yes_fill_price: float
    no_fill_price: float
    raw_spread_top_of_book: float   # 1 - yes_ask - no_ask
    raw_spread_depth_adjusted: float  # after slippage, before fees
    yes_fee_per_share: float
    no_fee_per_share: float
    fee_adjusted_spread: float      # after slippage AND fees (may be <= 0)


def evaluate_pair(
    a: Market,
    b: Market,
    now: datetime,
    *,
    buffer: float = SLIPPAGE_BUFFER,
    cap: int = MAX_FILLABLE_CAP,
    max_staleness: float = MAX_LEG_STALENESS_SECONDS,
    require_edge: bool = True,
) -> PairEvaluation | None:
    """Best-direction funnel record, or None if stale / no top-of-book edge.

    `require_edge=False` returns the best-direction evaluation even when the
    top-of-book edge is <= 0 (still None if stale / one-sided book) — used by
    the live logger to record the full spread distribution, not just windows.

    Picks the direction with the higher fee-adjusted spread. When a direction
    clears the fee gate (`max_fillable_size` size > 0) the fields are the real
    depth-walked economics. When it does not, the hedge is evaluated at a single
    contract (no slippage), so `raw_spread_depth_adjusted == raw_spread_top_of_book`
    and `fee_adjusted_spread` is top-of-book minus fees — the funnel then
    correctly attributes the loss to fees rather than slippage.
    """
    if a.age_seconds(now) > max_staleness or b.age_seconds(now) > max_staleness:
        return None

    best: PairEvaluation | None = None
    for yes_m, no_m in ((a, b), (b, a)):
        ya, na = yes_m.yes_ask, no_m.no_ask
        if ya is None or na is None:
            continue
        raw_top = 1.0 - ya - na
        yes_fee_fn = _TAKER_FEE[yes_m.platform]
        no_fee_fn = _TAKER_FEE[no_m.platform]
        plan = max_fillable_size(
            yes_m.yes_depth, no_m.no_depth, yes_fee_fn, no_fee_fn, buffer, cap
        )
        if plan.size > 0:
            ev = PairEvaluation(
                yes_platform=yes_m.platform, no_platform=no_m.platform,
                yes_ask_top=ya, no_ask_top=na,
                estimated_fillable_size=plan.size,
                yes_fill_price=plan.avg_yes_fill, no_fill_price=plan.avg_no_fill,
                raw_spread_top_of_book=raw_top,
                raw_spread_depth_adjusted=1.0 - plan.avg_yes_fill - plan.avg_no_fill,
                yes_fee_per_share=plan.yes_fee_per_share,
                no_fee_per_share=plan.no_fee_per_share,
                fee_adjusted_spread=plan.fee_adjusted_spread,
            )
        else:
            # not fee-viable at any size — evaluate one contract (no slippage)
            yfee, nfee = yes_fee_fn(ya), no_fee_fn(na)
            ev = PairEvaluation(
                yes_platform=yes_m.platform, no_platform=no_m.platform,
                yes_ask_top=ya, no_ask_top=na, estimated_fillable_size=0,
                yes_fill_price=ya, no_fill_price=na,
                raw_spread_top_of_book=raw_top,
                raw_spread_depth_adjusted=raw_top,
                yes_fee_per_share=yfee, no_fee_per_share=nfee,
                fee_adjusted_spread=raw_top - yfee - nfee,
            )
        if best is None or ev.fee_adjusted_spread > best.fee_adjusted_spread:
            best = ev

    if best is None:
        return None  # stale or one-sided book on both directions
    if require_edge and best.raw_spread_top_of_book <= 0:
        return None  # no apparent edge — caller only wants windows
    return best

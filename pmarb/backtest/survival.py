"""Window survival — was the edge still there Δ after we saw it?

Phase 1 reported how long windows LAST. That is not the question a trader asks.
The question is: at the moment we could have acted, was the opportunity still
open. Those differ because acting is not free — detection takes time, and the
other venue's book is already some age when we evaluate it.

So this measures, for every window that opened, whether the pair was STILL
viable Δ later, for a range of Δ including the latency this stack actually
measured on itself (`detect_latency_ms`).

Reconstructing state at t+Δ relies on how the collector logs (see main.py):

  * every evaluation while viable is written, unthrottled — so an open window
    is sampled at the rate the books update, not at a timer;
  * the first NON-viable evaluation after a viable one is written too, which
    pins the close;
  * so viability persists until a row says otherwise, and "the most recent row
    at or before t+Δ" is a faithful reading of the state then.

CENSORING IS NOT CLOSURE. If a pair has no row after t+Δ at all — the run
ended, the market delisted, the database was down — then what happened is
unknown, and counting it as "closed" would manufacture a decay that was really
the edge of the dataset. Those are reported separately and excluded from the
rate, the same discipline the funnel uses for unreviewed pairs.

An UNSUBSCRIBED row is the same kind of edge, deliberately marked as such: the
daily match refresh dropped the pair while the window was open, so the window
has an end (which is what pins its duration) but nobody observed the edge
disappear. Reading it as an observed close would report OUR OWN unsubscribe as
market decay, which is why the collector writes it under a reason of its own
rather than reusing the window-close marker.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from pmarb.models.sample import UNSUBSCRIBED

# Deltas worth reporting. The small ones bracket this stack's own measured
# latency (p50 ~0.2ms, p99 ~5ms); the large ones are what a human or a slower
# path would need.
DEFAULT_DELTAS_MS = (0, 250, 1_000, 5_000, 30_000, 60_000)


@dataclass(frozen=True, slots=True)
class SurvivalPoint:
    delta_ms: int
    opened: int          # windows whose fate at t+delta is known
    survived: int        # still viable then
    censored: int        # no observation covers t+delta — fate unknown
    pct: float | None    # survived / opened, None when nothing is known


def _viable(sample) -> bool:
    return sample.fillable_size > 0 and sample.spread_fee_adj > 0


def survival_curve(rows, deltas_ms=DEFAULT_DELTAS_MS) -> list[SurvivalPoint]:
    """For each Δ, the share of opened windows still viable Δ later.

    A window "opens" on a viable sample whose predecessor for that pair was not
    viable (or did not exist) — the same event the funnel counts as a window.
    """
    by_pair: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_pair[r.pair].append(r)
    for series in by_pair.values():
        series.sort(key=lambda s: s.observed_at)

    opens: list[tuple[list, int]] = []
    for series in by_pair.values():
        prev_viable = False
        for i, s in enumerate(series):
            now_viable = _viable(s)
            if now_viable and not prev_viable:
                opens.append((series, i))
            prev_viable = now_viable

    points: list[SurvivalPoint] = []
    for delta in deltas_ms:
        survived = censored = known = 0
        for series, i in opens:
            target = series[i].observed_at.timestamp() + delta / 1000.0
            # The last row at or before the target instant is the state then:
            # viability holds until an evaluation says otherwise, and an
            # evaluation only happens when a book moves.
            state = None
            for s in series[i:]:
                if s.observed_at.timestamp() <= target:
                    state = s
                else:
                    break
            # Our own unsubscribe, not the market's doing: the pair was still
            # viable when the refresh dropped it, so its fate at the target is
            # unknowable and counting the marker as a close would manufacture
            # decay. Same treatment as running out of data, because it IS
            # running out of data — just at a boundary we created.
            if state is not None and state.sample_reason == UNSUBSCRIBED:
                censored += 1
                continue
            still_open = state is not None and _viable(state)
            # Censoring applies only to a window that was STILL OPEN when the
            # data ran out — there, whether it would have survived to the target
            # is unknowable. A window already observed shut is known to be shut:
            # reopening would take an evaluation, and no rows means none
            # happened. Censoring the shut ones too would discard the very
            # outcomes the curve is measuring.
            if still_open and series[-1].observed_at.timestamp() < target:
                censored += 1
                continue
            known += 1
            if still_open:
                survived += 1
        points.append(SurvivalPoint(
            delta_ms=delta, opened=known, survived=survived, censored=censored,
            pct=round(100.0 * survived / known, 1) if known else None,
        ))
    return points


def latency_summary(rows) -> dict:
    """What the stack measured about its own timing, over the same rows.

    Two numbers that answer different questions: how long WE took to see the
    edge, and how old the OTHER leg's book already was when we did. The second
    is consistently three orders of magnitude larger, which is the point — a
    hedge is only as fresh as its staler leg, and that staleness is a property
    of two venues updating independently, not of this code.
    """
    detect = sorted(r.detect_latency_ms for r in rows
                    if r.detect_latency_ms is not None)
    partner = sorted(r.partner_age_ms for r in rows
                     if r.partner_age_ms is not None)

    def q(xs: list, p: float):
        if not xs:
            return None
        return xs[min(int(p * len(xs)), len(xs) - 1)]

    return {
        "rows_with_latency": len(detect),
        "rows_without_latency": sum(1 for r in rows
                                    if r.detect_latency_ms is None),
        "detect_ms": {"p50": q(detect, 0.50), "p99": q(detect, 0.99),
                      "max": detect[-1] if detect else None},
        "partner_age_ms": {"p50": q(partner, 0.50), "p95": q(partner, 0.95),
                           "max": partner[-1] if partner else None},
    }

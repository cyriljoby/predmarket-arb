"""Read-only FastAPI service over the observation store. No auth, one process.

Handlers are `def`, not `async def`: psycopg is a blocking driver, and FastAPI
runs `def` handlers in a threadpool so they cannot stall the event loop.

`verified_only` defaults to FALSE — only 152 of 3,025 pairs carry a review
verdict, so the unfiltered view is the exploratory one. Every row carries
`resolution_match`; anything quoted as a finding needs verified_only=true.

Polling, not push. SSE can be added later against the same queries.

Run it:  uvicorn pmarb.api.app:app --reload   (docs at /docs)
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query

from pmarb.api import queries as q
from pmarb.config import MAX_FILLABLE_CAP
from pmarb.db.engine import connect

app = FastAPI(
    title="predmarket-arb",
    version="0.1.0",
    summary="Read-only view of detected cross-venue arbitrage windows.",
    description=__doc__,
)


def _all(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with connect() as conn:
        return [q.jsonable(r) for r in conn.execute(sql, params).fetchall()]


def _one(sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    with connect() as conn:
        return q.jsonable(conn.execute(sql, params).fetchone())


@app.get("/health", tags=["meta"])
def health() -> dict[str, Any]:
    """Table counts and the age of the newest observation.

    `latest_observation` is the useful field: if it stops advancing, the
    collector has died or lost its feed, and every other number here is stale.
    """
    return _one(q.HEALTH, {})


@app.get("/opportunities", tags=["opportunities"])
def opportunities(
    within_seconds: int = Query(120, ge=1, le=86_400,
                                description="How recent an observation must be "
                                            "to count as still open."),
    verified_only: bool = Query(False, description="Restrict to pairs reviewed "
                                                   "as resolution_match=true. "
                                                   "Off by default: unreviewed "
                                                   "pairs are candidates, not "
                                                   "verified arbitrage."),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Currently-open viable windows, widest fee-adjusted edge first.

    Unreviewed pairs are INCLUDED by default; check `resolution_match` on each
    row before treating one as a hedge. Pass verified_only=true for the
    reviewed-only view.

    A window is open when the pair's MOST RECENT observation is both viable and
    recent. An older viable row means a window was open then, not now — and
    since the collector writes a close marker on the first non-viable
    evaluation, a stale row usually means the pair simply stopped updating.
    """
    return _all(q.LIVE, {"within": within_seconds,
                         "verified_only": verified_only, "limit": limit})


@app.get("/pairs/{pair_id}", tags=["opportunities"])
def pair(pair_id: int) -> dict[str, Any]:
    """One matched pair: both markets, the review verdict, and lifetime stats.

    The review fields are the point of this endpoint. `resolution_match=false`
    means the two markets do not settle on the same event, so any edge shown
    against it is two independent bets rather than a hedge.
    """
    row = _one(q.PAIR, {"pair_id": pair_id})
    if row is None:
        raise HTTPException(status_code=404, detail=f"no match_pair {pair_id}")
    return {**row, "stats": _one(q.PAIR_STATS, {"pair_id": pair_id})}


@app.get("/stats/funnel", tags=["stats"])
def funnel() -> list[dict[str, Any]]:
    """Attrition per observation, split by review verdict.

    Ordered the way the computation runs: top-of-book edge, then what survives
    slippage, then what survives fees. The `null` verdict row is the unreviewed
    backlog — it is reported, never merged into the true row.
    """
    return _all(q.FUNNEL, {})


@app.get("/stats/frontier", tags=["stats"])
def frontier(verified_only: bool = Query(False)) -> dict[str, Any]:
    """Average edge at quarter-points of the depth walk vs. at its end.

    The final point is the largest size that still cleared the gate, so it sits
    pinned just above SLIPPAGE_BUFFER by construction. The p25/p50/p75 columns
    are what the edge actually was at smaller size. `pinned_at_cap` counts rows
    where MAX_FILLABLE_CAP bound the search rather than the book — for those,
    the true fillable size is unknown and larger.
    """
    return _one(q.FRONTIER, {"verified_only": verified_only,
                             "cap": MAX_FILLABLE_CAP})


@app.get("/stats/horizon", tags=["stats"])
def horizon(verified_only: bool = Query(False)) -> list[dict[str, Any]]:
    """Edge by time to resolution, annualised.

    The headline number of the whole project lives here. A 1.3c edge on a
    contract settling in 2028 is not a 1.3% return; it is capital locked for
    years at well under cash. Absolute edge without a horizon flatters the
    long-dated outrights that dominate the catalog.
    """
    return _all(q.HORIZON, {"verified_only": verified_only})

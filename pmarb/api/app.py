"""FastAPI service over the observation store.

Read-only, no auth, single process — this serves one person looking at their
own collector's output, and pretending otherwise would be architecture theatre.

TWO DELIBERATE CHOICES, both about honesty rather than mechanics:

1. Endpoints are plain `def`, not `async def`. psycopg's sync driver blocks,
   and blocking the event loop is exactly the failure the collector's writer
   was built to avoid. FastAPI runs `def` handlers in a threadpool, so the
   blocking call is isolated. Making these `async def` while calling sync
   psycopg inside would look more modern and be strictly worse.

2. `verified_only` defaults to TRUE everywhere it appears. An unreviewed pair
   is a candidate, not an opportunity; Phase 1's whole finding was that most
   apparent edge dies at the matching layer, and a default that folded
   unreviewed pairs into the headline would re-tell the lie the review process
   exists to prevent. Callers can pass verified_only=false explicitly.

Polling, not push: the collector's write path is already decoupled from
delivery, so SSE/WebSocket can be added later against the same queries without
touching detection.

Run it:  uvicorn pmarb.api.app:app --reload
Docs:    http://127.0.0.1:8000/docs
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
    verified_only: bool = Query(True, description="Restrict to pairs reviewed "
                                                  "as resolution_match=true."),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Currently-open viable windows, widest fee-adjusted edge first.

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
def frontier(verified_only: bool = Query(True)) -> dict[str, Any]:
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
def horizon(verified_only: bool = Query(True)) -> list[dict[str, Any]]:
    """Edge by time to resolution, annualised.

    The headline number of the whole project lives here. A 1.3c edge on a
    contract settling in 2028 is not a 1.3% return; it is capital locked for
    years at well under cash. Absolute edge without a horizon flatters the
    long-dated outrights that dominate the catalog.
    """
    return _all(q.HORIZON, {"verified_only": verified_only})

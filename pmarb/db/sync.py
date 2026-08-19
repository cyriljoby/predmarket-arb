"""Seed and refresh the slow-changing tables from the matcher's output.

Both functions are idempotent upserts, so they run on every collector startup
and after every match refresh. Nothing is ever deleted: a pair the matcher stops
producing is RETRACTED, not removed, because observations already reference it
and the backtest needs to know a pair was once believed in.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass

from psycopg.types.json import Jsonb

from pmarb.feeds._util import now_utc


def _structured_id(market) -> Jsonb | None:
    for attr in ("event", "futures"):
        val = getattr(market, attr, None)
        if val is not None:
            return Jsonb({"kind": attr, **(asdict(val) if is_dataclass(val) else {})},
                         dumps=lambda o: json.dumps(o, default=str))
    return None


def upsert_markets(conn, markets) -> int:
    """Insert or refresh venue market metadata.

    first_seen is preserved on conflict; last_seen advances. Question text and
    resolution date are refreshed because venues amend both — a postponed game
    gets a new settlement date, which is exactly why observations snapshot their
    own copy rather than joining here.
    """
    now = now_utc()
    rows = [(m.id, m.platform, m.question, m.resolution_date,
             m.category or None, _structured_id(m), now)
            for m in markets]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO market (id, venue, question, resolution_date, category,
                                structured_id, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                question        = EXCLUDED.question,
                resolution_date = EXCLUDED.resolution_date,
                category        = EXCLUDED.category,
                structured_id   = EXCLUDED.structured_id,
                last_seen       = EXCLUDED.last_seen
            """,
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[6]) for r in rows],
        )
    return len(rows)


def upsert_match_pairs(conn, matches: list[dict],
                       reviews: list[dict] | None = None) -> dict[str, int]:
    """Insert or refresh match pairs, and retract those no longer produced.

    Pairs referencing a market not yet in the DB are skipped and counted; seed
    markets first.

    Review fields come from `reviews` keyed by (kalshi_id, polymarket_id) and are
    written VERBATIM — including an explicit None. A withdrawn verdict is a
    decision, not an absence; treating it as missing is how 21 withdrawn labels
    silently came back once already.
    """
    by_pair = {(r["kalshi_id"], r["polymarket_id"]): r for r in (reviews or [])}
    now = now_utc()

    # The match set and the market catalog are fetched at different moments and
    # both venues churn constantly, so some pairs reference markets that have
    # already expired. Skip those rather than fail the whole seed — but COUNT
    # them, because a large skip means the match set has gone stale and should
    # be regenerated, which is not something to discover silently.
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM market")
        known = {r["id"] for r in cur.fetchall()}
    usable = [m for m in matches
              if m["kalshi_id"] in known and m["polymarket_id"] in known]
    skipped = len(matches) - len(usable)

    rows = []
    for m in usable:
        rv = by_pair.get((m["kalshi_id"], m["polymarket_id"]), {})
        rows.append((
            m["kalshi_id"], m["polymarket_id"], m["match_method"],
            m["similarity_score"], m.get("resolution_date_delta_days"),
            rv.get("resolution_match"), rv.get("cohort"), rv.get("standard"),
            rv.get("notes") or None, rv.get("reviewer"), now,
        ))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO match_pair (
                market_a_id, market_b_id, match_method, similarity_score,
                date_delta_days, resolution_match, review_cohort,
                review_standard, review_notes, reviewer, first_seen)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (market_a_id, market_b_id) DO UPDATE SET
                match_method      = EXCLUDED.match_method,
                similarity_score  = EXCLUDED.similarity_score,
                date_delta_days   = EXCLUDED.date_delta_days,
                resolution_match  = EXCLUDED.resolution_match,
                review_cohort     = EXCLUDED.review_cohort,
                review_standard   = EXCLUDED.review_standard,
                review_notes      = EXCLUDED.review_notes,
                reviewer          = EXCLUDED.reviewer,
                retracted_at      = NULL,      -- re-produced, so un-retract
                retraction_reason = NULL
            """,
            rows,
        )
        live = {(m["kalshi_id"], m["polymarket_id"]) for m in usable}
        cur.execute("SELECT id, market_a_id, market_b_id FROM match_pair "
                    "WHERE retracted_at IS NULL")
        gone = [r["id"] for r in cur.fetchall()
                if (r["market_a_id"], r["market_b_id"]) not in live]
        if gone:
            cur.execute("UPDATE match_pair SET retracted_at = %s "
                        "WHERE id = ANY(%s)", (now, gone))
    return {"upserted": len(rows), "retracted": len(gone), "skipped": skipped}


def pair_ids(conn) -> dict[tuple[str, str], int]:
    """(market_a_id, market_b_id) -> match_pair.id, for the hot write path."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, market_a_id, market_b_id FROM match_pair")
        return {(r["market_a_id"], r["market_b_id"]): r["id"] for r in cur.fetchall()}

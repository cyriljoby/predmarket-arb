"""Database-backed inputs for the backtest.

The database is the canonical source, so this is a near-passthrough: column
names already match `Sample`'s field names, and the only real work is turning
`numeric` into `float` at the boundary. The JSONL reader is the adapter now,
not this.
"""

from __future__ import annotations

from datetime import datetime

from pmarb.models import Sample


def _f(v) -> float | None:
    """numeric arrives as Decimal; the funnel does float arithmetic on it.

    Converting once at the boundary keeps Decimal/float mixing from surfacing
    somewhere deep inside a percentage.
    """
    return float(v) if v is not None else None


def _to_sample(r: dict) -> Sample:
    return Sample(
        observed_at=r["observed_at"],
        market_a_id=r["market_a_id"],
        market_b_id=r["market_b_id"],
        spread_top=_f(r["spread_top"]),
        spread_depth=_f(r["spread_depth"]),
        spread_fee_adj=_f(r["spread_fee_adj"]),
        fillable_size=r["fillable_size"],
        question=r["question"] or "",
        match_method=r["match_method"],
        yes_venue=r["yes_venue"],
        sample_reason=r["sample_reason"],
        ask_top_yes=_f(r["yes_ask_top"]), ask_top_no=_f(r["no_ask_top"]),
        fill_yes=_f(r["yes_fill_price"]), fill_no=_f(r["no_fill_price"]),
        fee_yes=_f(r["yes_fee"]), fee_no=_f(r["no_fee"]),
        resolution_date_a=r["resolution_date_a"],
        resolution_date_b=r["resolution_date_b"],
    )


def load_from_db(conn, *, since: datetime | None = None,
                 until: datetime | None = None) -> tuple[list, list, set]:
    """Return (rows, matches, tracked) for `run_backtest`.

    `tracked` is every pair the collector monitored in the period, which is NOT
    the same as every pair that produced rows — a pair that never went viable
    and never changed edge writes nothing. Conflating the two is what made the
    Phase 1 futures numbers floors, so the denominator is taken from match_pair
    rather than inferred from the observations.
    """
    # Conditions are assembled from a fixed set of fragments with bound
    # parameters — never interpolated values — so the filter stays composable
    # without becoming an injection surface.
    where, params = ["1=1"], []
    if since:
        where.append("o.observed_at >= %s")
        params.append(since)
    if until:
        where.append("o.observed_at < %s")
        params.append(until)
    clause = " AND ".join(where)

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT o.*, mp.market_a_id, mp.market_b_id, mp.match_method,
                   mp.resolution_match, ma.question
              FROM observation o
              JOIN match_pair mp ON mp.id = o.match_pair_id
              JOIN market ma     ON ma.id = mp.market_a_id
             WHERE {clause}
        """, params)
        rows = [_to_sample(r) for r in cur.fetchall()]

        # The match set as it stands NOW, in the shape the backtest expects.
        # Retracted pairs are included so the backtest can drop their samples
        # from every numerator and denominator, exactly as the file path does.
        cur.execute("""
            SELECT market_a_id, market_b_id, match_method, similarity_score,
                   date_delta_days, resolution_match, review_cohort,
                   review_notes, reviewer, retracted_at
              FROM match_pair
        """)
        pairs = cur.fetchall()

    matches = [{
        "kalshi_id": p["market_a_id"],
        "polymarket_id": p["market_b_id"],
        "match_method": p["match_method"],
        "similarity_score": float(p["similarity_score"]),
        "resolution_date_delta_days": p["date_delta_days"],
        "resolution_match": p["resolution_match"],
        "review_sample": p["review_cohort"],
        "resolution_notes": p["review_notes"] or "",
        "verified_by": p["reviewer"],
    } for p in pairs if p["retracted_at"] is None]

    tracked = {(p["market_a_id"], p["market_b_id"])
               for p in pairs if p["retracted_at"] is None}
    return rows, matches, tracked

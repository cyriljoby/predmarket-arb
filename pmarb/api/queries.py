"""SQL for the API, kept apart from the routes so each query can be run alone.

Read-only by construction — there is no write path anywhere in the API.

`resolution_match` is the load-bearing field: an unreviewed pair is a candidate,
not an arb, and most apparent edge dies at the matching layer. `verified_only`
filters on it and defaults to FALSE (see app.py).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# One viable observation per pair, most recent first. "Currently open" means
# the LATEST observation for that pair is viable and recent — an older viable
# row says a window WAS open, not that it is.
LIVE = """
WITH latest AS (
    SELECT DISTINCT ON (match_pair_id) *
    FROM observation
    WHERE observed_at > now() - make_interval(secs => %(within)s)
    ORDER BY match_pair_id, observed_at DESC
)
SELECT o.match_pair_id, o.observed_at, o.yes_venue,
       o.fillable_size, o.spread_top, o.spread_depth, o.spread_fee_adj,
       o.size_at_p25, o.edge_at_p25, o.size_at_p50, o.edge_at_p50,
       o.size_at_p75, o.edge_at_p75,
       o.resolution_date_a AS resolution_date,
       mp.match_method, mp.resolution_match, mp.similarity_score,
       ma.question AS question_a, mb.question AS question_b
FROM latest o
JOIN match_pair mp ON mp.id = o.match_pair_id
JOIN market ma ON ma.id = mp.market_a_id
JOIN market mb ON mb.id = mp.market_b_id
WHERE o.fillable_size > 0
  AND mp.retracted_at IS NULL
  AND (%(verified_only)s IS FALSE OR mp.resolution_match IS TRUE)
ORDER BY o.spread_fee_adj DESC
LIMIT %(limit)s
"""

PAIR = """
SELECT mp.id, mp.match_method, mp.similarity_score, mp.date_delta_days,
       mp.resolution_match, mp.review_cohort, mp.review_standard,
       mp.review_notes, mp.reviewer, mp.retracted_at,
       ma.id AS market_a_id, ma.question AS question_a, ma.venue AS venue_a,
       mb.id AS market_b_id, mb.question AS question_b, mb.venue AS venue_b
FROM match_pair mp
JOIN market ma ON ma.id = mp.market_a_id
JOIN market mb ON mb.id = mp.market_b_id
WHERE mp.id = %(pair_id)s
"""

PAIR_STATS = """
SELECT count(*) AS observations,
       count(*) FILTER (WHERE fillable_size > 0) AS viable_observations,
       min(observed_at) AS first_seen,
       max(observed_at) AS last_seen,
       max(spread_fee_adj) FILTER (WHERE fillable_size > 0) AS best_edge,
       avg(spread_fee_adj) FILTER (WHERE fillable_size > 0) AS avg_edge,
       max(fillable_size) AS max_size
FROM observation WHERE match_pair_id = %(pair_id)s
"""

# The honest funnel, in the computation order: slippage first, fees second.
# Denominator is verified-true pairs only; the unreviewed count sits beside it
# so the gap stays visible instead of inflating the rate.
FUNNEL = """
SELECT mp.resolution_match,
       count(DISTINCT mp.id) AS pairs,
       count(*) AS observations,
       count(*) FILTER (WHERE o.spread_top > 0) AS had_top_of_book_edge,
       count(*) FILTER (WHERE o.spread_depth > 0) AS survived_slippage,
       count(*) FILTER (WHERE o.spread_fee_adj > 0 AND o.fillable_size > 0)
           AS survived_fees
FROM observation o
JOIN match_pair mp ON mp.id = o.match_pair_id
GROUP BY 1 ORDER BY 1 NULLS LAST
"""

# Why the frontier columns exist. The final point of the walk is the edge at
# the largest size that still cleared the gate, so it sits pinned just above
# SLIPPAGE_BUFFER on every row — a property of the buffer, not the market.
# The quarter-points show what the edge actually was at a smaller size.
FRONTIER = """
SELECT count(*) AS observations,
       avg(edge_at_p25) AS avg_edge_p25, avg(size_at_p25) AS avg_size_p25,
       avg(edge_at_p50) AS avg_edge_p50, avg(size_at_p50) AS avg_size_p50,
       avg(edge_at_p75) AS avg_edge_p75, avg(size_at_p75) AS avg_size_p75,
       avg(spread_fee_adj) AS avg_edge_final, avg(fillable_size) AS avg_size_final,
       count(*) FILTER (WHERE fillable_size >= %(cap)s) AS pinned_at_cap
FROM observation o
JOIN match_pair mp ON mp.id = o.match_pair_id
WHERE o.size_at_p25 IS NOT NULL
  AND (%(verified_only)s IS FALSE OR mp.resolution_match IS TRUE)
"""

# A cent of edge is not a cent of return: these contracts settle years out.
# Dividing by the holding period is what turns an apparent 1.3c arb on a 2028
# election contract into 0.6%/yr, which is below cash.
HORIZON = """
SELECT CASE
         WHEN o.resolution_date_a < now() + interval '90 days' THEN 'under_90d'
         WHEN o.resolution_date_a < now() + interval '1 year'  THEN '90d_to_1y'
         ELSE 'over_1y' END AS horizon,
       count(DISTINCT o.match_pair_id) AS pairs,
       count(*) AS observations,
       avg(o.spread_fee_adj) AS avg_edge,
       max(o.spread_fee_adj) AS best_edge,
       -- 1/50th of a year floors the divisor so a contract settling tomorrow
       -- cannot report an absurd annualised figure.
       avg(o.spread_fee_adj / greatest(
             extract(epoch FROM (o.resolution_date_a - now())) / 31557600, 0.02)
       ) AS avg_annualised
FROM observation o
JOIN match_pair mp ON mp.id = o.match_pair_id
WHERE o.fillable_size > 0
  AND o.resolution_date_a IS NOT NULL
  AND (%(verified_only)s IS FALSE OR mp.resolution_match IS TRUE)
-- Order by the bucket boundary, not the label: alphabetically "under_90d"
-- sorts last, which reads as if the near-dated bucket were the tail.
GROUP BY 1 ORDER BY min(o.resolution_date_a)
"""

HEALTH = """
SELECT (SELECT count(*) FROM market) AS markets,
       (SELECT count(*) FROM match_pair WHERE retracted_at IS NULL) AS match_pairs,
       (SELECT count(*) FROM match_pair WHERE resolution_match IS TRUE)
           AS verified_pairs,
       (SELECT count(*) FROM observation) AS observations,
       (SELECT max(observed_at) FROM observation) AS latest_observation
"""


def jsonable(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decimal -> float for the whole row.

    Postgres numeric arrives as Decimal, which pydantic renders as a JSON
    STRING. Edges are compared numerically by every consumer, so a quoted
    "0.00280" would be a trap; the money-safe Decimal matters in the database,
    not on the wire.
    """
    if row is None:
        return None
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}

"""core schema: market, match_pair, observation

Revision ID: c8d2a04273b8
Revises: 
Create Date: 2026-08-18 22:39:21.435226

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d2a04273b8'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- market -------------------------------------------------------- #
    # One row per venue market. Slow-changing: refreshed on discovery, not on
    # every book update.
    op.execute("""
        CREATE TABLE market (
            id              text PRIMARY KEY,
            venue           text NOT NULL
                            CHECK (venue IN ('kalshi', 'polymarket_us')),
            question        text NOT NULL,
            resolution_date timestamptz,
            category        text,
            -- SportsEvent / FuturesEvent as the feed extracted it. Kept as
            -- jsonb because the two shapes differ and neither is queried
            -- structurally today.
            structured_id   jsonb,
            first_seen      timestamptz NOT NULL DEFAULT now(),
            last_seen       timestamptz NOT NULL DEFAULT now()
        )
    """)

    # ---- match_pair ----------------------------------------------------- #
    # Venue-generic on purpose (market_a/market_b, not kalshi_/poly_) so a
    # third venue does not require a migration.
    #
    # Review lives here as columns rather than its own table for now. That is
    # coherent ONLY because match_pair is unversioned: a review is about two
    # markets being the same contract, which outlives any match-set version, so
    # the moment versioning arrives the review has to move. cohort and standard
    # are carried from day one — pooling a viability-selected cohort with a
    # random sample is the exact error the Phase 1 report shipped, and separate
    # columns make the honest query the easy one.
    op.execute("""
        CREATE TABLE match_pair (
            id                bigserial PRIMARY KEY,
            market_a_id       text NOT NULL REFERENCES market(id),
            market_b_id       text NOT NULL REFERENCES market(id),
            match_method      text NOT NULL
                              CHECK (match_method IN ('structured','futures','lexical')),
            similarity_score  numeric(6,4) NOT NULL,
            date_delta_days   integer,

            resolution_match  boolean,       -- NULL = unreviewed OR unreviewable
            review_cohort     text,          -- NEVER pool cohorts when reporting
            review_standard   text,          -- 'question-level' | 'rules-level'
            review_notes      text,
            reviewer          text,

            retracted_at      timestamptz,   -- set when the matcher withdraws it
            retraction_reason text
                              CHECK (retraction_reason IS NULL OR retraction_reason IN
                                     ('lost_on_score','lost_on_delta','refused_ambiguous')),
            first_seen        timestamptz NOT NULL DEFAULT now(),
            UNIQUE (market_a_id, market_b_id)
        )
    """)
    op.execute("CREATE INDEX match_pair_live_idx ON match_pair (id) "
               "WHERE retracted_at IS NULL")
    op.execute("CREATE INDEX match_pair_market_b_idx ON match_pair (market_b_id)")

    # ---- observation ---------------------------------------------------- #
    # The hot table. Range-partitioned monthly on observed_at so retention is
    # DROP PARTITION rather than a bulk DELETE, and each partition's indexes
    # stay small enough to cache.
    #
    # numeric, not double precision: the fee model is sub-cent sensitive
    # (0.0170 at top-of-book vs 0.0172 at the depth-walked fill) and float drift
    # would accumulate in exactly the AVG/SUM the reports run.
    #
    # Both legs' resolution dates are captured HERE, not joined from market,
    # because venues amend them — Poly's own rules say a postponed game's
    # settlement date is rewritten. Joining later would silently restate every
    # historical horizon. Capital is locked until BOTH legs settle, so any
    # holding-period return uses the later of the two.
    op.execute("""
        CREATE TABLE observation (
            match_pair_id   bigint NOT NULL REFERENCES match_pair(id),
            observed_at     timestamptz NOT NULL,
            -- 0 heartbeat | 1 edge-change | 2 viable | 3 window close marker
            sample_reason   smallint NOT NULL CHECK (sample_reason BETWEEN 0 AND 3),
            yes_venue       text NOT NULL
                            CHECK (yes_venue IN ('kalshi','polymarket_us')),

            yes_ask_top     numeric(6,4),
            no_ask_top      numeric(6,4),
            yes_fill_price  numeric(6,4),
            no_fill_price   numeric(6,4),
            yes_fee         numeric(7,5),
            no_fee          numeric(7,5),

            spread_top      numeric(7,5) NOT NULL,
            spread_depth    numeric(7,5) NOT NULL,
            spread_fee_adj  numeric(7,5) NOT NULL,
            fillable_size   integer NOT NULL,

            resolution_date_a timestamptz,   -- as at observation time
            resolution_date_b timestamptz,

            PRIMARY KEY (match_pair_id, observed_at)
        ) PARTITION BY RANGE (observed_at)
    """)
    # BRIN over append-only time-ordered data is kilobytes where a btree is
    # gigabytes, and full-range scans are the backtest's access pattern.
    op.execute("CREATE INDEX observation_observed_brin ON observation "
               "USING brin (observed_at)")
    op.execute("CREATE INDEX observation_viable_idx ON observation "
               "(match_pair_id, observed_at) WHERE fillable_size > 0")

    # Partitions are created ahead of time by pmarb.db.schema.ensure_partitions;
    # seed the current and next month so a fresh database can accept writes.
    op.execute("""
        DO $$
        DECLARE m date := date_trunc('month', now())::date;
        BEGIN
          FOR i IN 0..1 LOOP
            EXECUTE format(
              'CREATE TABLE IF NOT EXISTS observation_%s PARTITION OF observation '
              'FOR VALUES FROM (%L) TO (%L)',
              to_char(m + (i || ' month')::interval, 'YYYY_MM'),
              (m + (i || ' month')::interval)::date,
              (m + ((i+1) || ' month')::interval)::date);
          END LOOP;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS observation CASCADE")
    op.execute("DROP TABLE IF EXISTS match_pair CASCADE")
    op.execute("DROP TABLE IF EXISTS market CASCADE")

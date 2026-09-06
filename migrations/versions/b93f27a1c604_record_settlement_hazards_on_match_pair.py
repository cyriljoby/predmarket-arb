"""record settlement hazards on match_pair

Revision ID: b93f27a1c604
Revises: a71c3f5d2e08
Create Date: 2026-09-05 19:41:02.118337

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b93f27a1c604'
down_revision: str | Sequence[str] | None = 'a71c3f5d2e08'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Known settlement divergences for a pair (see matching/hazards.py): ties,
    # walkover/void asymmetry, substitute reassignment. NOT a review verdict —
    # `resolution_match` stays the human call, and a hazard says only that the
    # hedge has a known hole.
    #
    # Stored here rather than derived at query time because the hazard TABLE
    # changes as review finds new mechanisms, and a report run months later must
    # reflect what was believed when the pair was streamed, not what the current
    # table says.
    #
    # text[] rather than a join table: the vocabulary is closed and tiny (three
    # values today), pairs carry zero or one in practice, and the query this
    # exists to serve is "how did flagged pairs behave", which is a filter, not
    # a relation. An empty array means "none in the table", which is NOT the
    # same as "none exist" — lines and props have no hazard rows at all yet.
    op.execute("ALTER TABLE match_pair ADD COLUMN settlement_hazards text[] "
               "NOT NULL DEFAULT '{}'")
    # Partial index: the interesting query is always the flagged minority (~4%
    # of pairs today), so indexing only those keeps it small.
    op.execute("CREATE INDEX match_pair_hazard_idx ON match_pair "
               "USING gin (settlement_hazards) "
               "WHERE cardinality(settlement_hazards) > 0")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS match_pair_hazard_idx")
    op.execute("ALTER TABLE match_pair DROP COLUMN IF EXISTS settlement_hazards")

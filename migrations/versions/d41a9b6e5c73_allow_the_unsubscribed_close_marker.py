"""allow the unsubscribed close marker

Revision ID: d41a9b6e5c73
Revises: c05e8b1a7d92
Create Date: 2026-09-21 23:10:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd41a9b6e5c73'
down_revision: str | Sequence[str] | None = 'c05e8b1a7d92'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # sample_reason 4 = UNSUBSCRIBED: the daily match refresh dropped this pair
    # while its window was still open, so the window has an end but nobody
    # observed the edge disappear.
    #
    # A distinct reason rather than reusing 3 (window close), because the two
    # are opposite facts. 3 means an evaluation found the edge gone — an
    # observation. 4 means we stopped looking — censoring. The survival curve
    # counts censored windows separately precisely so a dataset edge is never
    # reported as decay, and pinning our own unsubscribe as an observed close
    # would manufacture exactly that.
    #
    # Kept as an enumerated CHECK for the same reason the match_method one is:
    # the next reason added should fail the insert loudly rather than land as an
    # integer nobody has a meaning for.
    op.execute("ALTER TABLE observation "
               "DROP CONSTRAINT observation_sample_reason_check")
    op.execute("""
        ALTER TABLE observation ADD CONSTRAINT observation_sample_reason_check
        CHECK (sample_reason BETWEEN 0 AND 4)
    """)


def downgrade() -> None:
    # The marker rows have to go first, or the narrower constraint cannot be
    # restored. Losing them means those windows revert to unclosed, which is
    # what the marker exists to prevent — so this direction is lossy on purpose.
    op.execute("DELETE FROM observation WHERE sample_reason = 4")
    op.execute("ALTER TABLE observation "
               "DROP CONSTRAINT observation_sample_reason_check")
    op.execute("""
        ALTER TABLE observation ADD CONSTRAINT observation_sample_reason_check
        CHECK (sample_reason BETWEEN 0 AND 3)
    """)

"""allow line and prop match methods

Revision ID: c05e8b1a7d92
Revises: b93f27a1c604
Create Date: 2026-09-06 12:13:44.905112

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c05e8b1a7d92'
down_revision: str | Sequence[str] | None = 'b93f27a1c604'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Two matchers were added after this constraint was written, and the
    # constraint is what caught it — seeding failed outright rather than
    # silently dropping every spread, total and prop pair. Keeping the check
    # enumerated (rather than widening it to any text) is what made that
    # failure loud, so it stays enumerated.
    op.execute("ALTER TABLE match_pair DROP CONSTRAINT match_pair_match_method_check")
    op.execute("""
        ALTER TABLE match_pair ADD CONSTRAINT match_pair_match_method_check
        CHECK (match_method IN ('structured','futures','line','prop','lexical'))
    """)


def downgrade() -> None:
    # Rows using the new methods must go first, or the old constraint cannot
    # be restored.
    op.execute("DELETE FROM match_pair WHERE match_method IN ('line','prop')")
    op.execute("ALTER TABLE match_pair DROP CONSTRAINT match_pair_match_method_check")
    op.execute("""
        ALTER TABLE match_pair ADD CONSTRAINT match_pair_match_method_check
        CHECK (match_method IN ('structured','futures','lexical'))
    """)

"""record detection latency on observations

Revision ID: a71c3f5d2e08
Revises: fb25a78c2b51
Create Date: 2026-09-02 11:04:18.220117

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a71c3f5d2e08'
down_revision: str | Sequence[str] | None = 'fb25a78c2b51'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # How long this stack took to SEE the edge, and how stale the other leg
    # already was when it did.
    #
    # Phase 1 measured how long windows last and concluded they are too short
    # to capture at retail latency — while never recording what this system's
    # latency actually is. Half of that comparison was an assumption. These two
    # columns are the missing half, and they must be per-observation: the
    # survival curve asks "was the edge still there `detect_latency_ms` after
    # this update arrived", which a run-level average cannot answer.
    #
    # NULLable because a snapshot that never came off a stream (REST discovery,
    # a replayed row) has no receipt instant, and a zero there would read as
    # instantaneous detection rather than an unmeasured one.
    #
    # detect_latency_ms is measured on a MONOTONIC clock inside one process, so
    # microsecond resolution is real; partner_age_ms crosses two wall clocks and
    # is only meaningful to the millisecond.
    op.execute("ALTER TABLE observation ADD COLUMN detect_latency_ms numeric(9,3)")
    op.execute("ALTER TABLE observation ADD COLUMN partner_age_ms integer")


def downgrade() -> None:
    op.execute("ALTER TABLE observation DROP COLUMN IF EXISTS detect_latency_ms")
    op.execute("ALTER TABLE observation DROP COLUMN IF EXISTS partner_age_ms")

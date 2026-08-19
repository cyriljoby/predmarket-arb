"""record the size-edge frontier on observations

Revision ID: fb25a78c2b51
Revises: c8d2a04273b8
Create Date: 2026-08-19 15:03:01.609561

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'fb25a78c2b51'
down_revision: str | Sequence[str] | None = 'c8d2a04273b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The size/edge frontier, sampled at quarter-points of the walk.
    #
    # max_fillable_size walks both ladders adding shares at progressively worse
    # prices, so the AVERAGE edge decays as size grows. It reported only the
    # final point — the largest size that still cleared the gate — which meant
    # every opportunity was recorded at its own truncation point. The result:
    # a median edge pinned at 1.01c across every duration bucket, which is the
    # SLIPPAGE_BUFFER showing through rather than a property of the market.
    #
    # The intermediate points already exist inside the walk and were discarded.
    # They cannot be recovered later: order books live only in the collector's
    # memory and are never stored, so anything not captured here is gone.
    #
    # Sizes are stored alongside edges rather than derived from a percentage of
    # fillable_size. A bare percentile is not comparable across rows — p25 is
    # 250 shares on one and 3 on another — and the query this exists to serve
    # ("edge above 2c at a size worth trading") needs the absolute number.
    #
    # p100 is deliberately absent: that is (fillable_size, spread_fee_adj).
    for pct in (25, 50, 75):
        op.execute(f"ALTER TABLE observation ADD COLUMN size_at_p{pct} integer")
        op.execute(f"ALTER TABLE observation ADD COLUMN edge_at_p{pct} numeric(7,5)")


def downgrade() -> None:
    for pct in (25, 50, 75):
        op.execute(f"ALTER TABLE observation DROP COLUMN IF EXISTS size_at_p{pct}")
        op.execute(f"ALTER TABLE observation DROP COLUMN IF EXISTS edge_at_p{pct}")

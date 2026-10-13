"""NSE participant-wise F&O open interest (#317): fno_participant_oi

Revision ID: c7a3e9d1f5b2
Revises: b5e1d7c3a9f4
Create Date: 2026-10-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a3e9d1f5b2"
down_revision: str | Sequence[str] | None = "b5e1d7c3a9f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COUNTS = ("fut_idx_long", "fut_idx_short", "fut_stk_long", "fut_stk_short", "opt_idx_call_long", "opt_idx_put_long",
          "opt_idx_call_short", "opt_idx_put_short", "opt_stk_call_long", "opt_stk_put_long", "opt_stk_call_short",
          "opt_stk_put_short", "total_long", "total_short")  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "fno_participant_oi",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("client_type", sa.String(length=10), nullable=False),
        *[sa.Column(c, sa.BigInteger(), nullable=False) for c in COUNTS],
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("day", "client_type"),
    )


def downgrade() -> None:
    op.drop_table("fno_participant_oi")

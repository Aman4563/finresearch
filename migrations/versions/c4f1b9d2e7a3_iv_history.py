"""iv history

Revision ID: c4f1b9d2e7a3
Revises: 9a4a02fd012e
Create Date: 2026-09-30 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4f1b9d2e7a3"
down_revision: str | Sequence[str] | None = "9a4a02fd012e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "iv_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("underlying", sa.Numeric(14, 2), nullable=True),
        sa.Column("atm_strike", sa.Numeric(14, 2), nullable=True),
        sa.Column("atm_iv", sa.Numeric(8, 3), nullable=False),
        sa.Column("call_iv", sa.Numeric(8, 3), nullable=True),
        sa.Column("put_iv", sa.Numeric(8, 3), nullable=True),
        sa.Column("skew_25d", sa.Numeric(8, 3), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "day"),
    )
    op.create_index(op.f("ix_iv_history_symbol"), "iv_history", ["symbol"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_iv_history_symbol"), table_name="iv_history")
    op.drop_table("iv_history")

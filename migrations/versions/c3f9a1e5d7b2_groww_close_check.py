"""Groww close check (#283): Groww's daily close against the exchange's official close, per stock-day

Revision ID: c3f9a1e5d7b2
Revises: b8e1d4c7a2f6
Create Date: 2026-10-09 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f9a1e5d7b2"
down_revision: str | Sequence[str] | None = "b8e1d4c7a2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "groww_close_check",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("exchange", sa.String(length=3), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("groww_close", sa.Numeric(14, 4), nullable=False),
        sa.Column("official_close", sa.Numeric(14, 4), nullable=False),
        sa.Column("official_source", sa.String(length=80), nullable=False),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day", "exchange", "symbol"),
    )
    op.create_index(op.f("ix_groww_close_check_day"), "groww_close_check", ["day"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_groww_close_check_day"), table_name="groww_close_check")
    op.drop_table("groww_close_check")

"""intraday series archive

Revision ID: d7a3e5c1f9b2
Revises: c3f1a9d2b7e4
Create Date: 2026-09-30 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7a3e5c1f9b2"
down_revision: str | Sequence[str] | None = "c3f1a9d2b7e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intraday_series",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("prev_close", sa.Numeric(14, 4), nullable=True),
        sa.Column("ticks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tick_count", sa.Integer(), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "symbol", "day"),
    )
    op.create_index(op.f("ix_intraday_series_symbol"), "intraday_series", ["symbol"], unique=False)
    op.create_index(op.f("ix_intraday_series_day"), "intraday_series", ["day"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_intraday_series_day"), table_name="intraday_series")
    op.drop_index(op.f("ix_intraday_series_symbol"), table_name="intraday_series")
    op.drop_table("intraday_series")

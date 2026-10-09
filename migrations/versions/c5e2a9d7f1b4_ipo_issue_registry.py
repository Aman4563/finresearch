"""IPO issue registry (#292): current and upcoming NSE and BSE issues, refreshed by the monitor

Revision ID: c5e2a9d7f1b4
Revises: b8e1d4c7a2f6
Create Date: 2026-10-09 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5e2a9d7f1b4"
down_revision: str | Sequence[str] | None = "b8e1d4c7a2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ipo_issue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exchange", sa.String(length=5), nullable=False),
        sa.Column("issue_key", sa.String(length=40), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("board", sa.String(length=20), nullable=False),
        sa.Column("series", sa.String(length=10), nullable=True),
        sa.Column("open_date", sa.Date(), nullable=True),
        sa.Column("close_date", sa.Date(), nullable=True),
        sa.Column("price_band", sa.String(length=80), nullable=True),
        sa.Column("price_low", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("price_high", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("exchange_status", sa.String(length=40), nullable=True),
        sa.Column("duplicate_of", sa.Integer(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("off_list_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exchange", "issue_key"),
    )
    op.create_index(op.f("ix_ipo_issue_symbol"), "ipo_issue", ["symbol"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ipo_issue_symbol"), table_name="ipo_issue")
    op.drop_table("ipo_issue")

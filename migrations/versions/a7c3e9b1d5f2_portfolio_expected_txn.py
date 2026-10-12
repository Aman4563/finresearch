"""Expected SIP instalments between statements (#297): estimates only, never transactions

Revision ID: a7c3e9b1d5f2
Revises: c5e2a9d7f1b4
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e9b1d5f2"
down_revision: str | Sequence[str] | None = "c5e2a9d7f1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_expected_txn",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=False),
        sa.Column("expected_day", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("est_units", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("est_nav", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("nav_day", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("basis", sa.String(length=20), nullable=False),
        sa.Column("matched_txn_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["holding_id"], ["portfolio_holding.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matched_txn_id"], ["portfolio_txn.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("holding_id", "expected_day"),
    )
    op.create_index(
        op.f("ix_portfolio_expected_txn_holding_id"), "portfolio_expected_txn", ["holding_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_portfolio_expected_txn_holding_id"), table_name="portfolio_expected_txn")
    op.drop_table("portfolio_expected_txn")

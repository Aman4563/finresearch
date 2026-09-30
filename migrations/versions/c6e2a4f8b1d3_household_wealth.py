"""Household finances (/wealth): manual assets and their dated valuations, loans, goals and insurance policies

Five new tables; nothing existing changes. Personal data entered by hand, local only, never sent to an LLM; no
account numbers or identity data (a bank or lender label is enough).

Revision ID: c6e2a4f8b1d3
Revises: b3e9d1f5a7c2
Create Date: 2026-09-30 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6e2a4f8b1d3"
down_revision: str | Sequence[str] | None = "b3e9d1f5a7c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "wealth_asset",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("institution", sa.String(length=120), nullable=True),
        sa.Column("asset_class", sa.String(length=20), nullable=True),
        sa.Column("principal", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("rate_pct", sa.Numeric(precision=7, scale=3), nullable=True),
        sa.Column("compounding", sa.Integer(), nullable=False),
        sa.Column("monthly_contribution", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("maturity_date", sa.Date(), nullable=True),
        sa.Column("liquid", sa.Boolean(), nullable=False),
        sa.Column("equity_pct", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _created(),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "wealth_valuation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["wealth_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "day"),
    )
    op.create_index(op.f("ix_wealth_valuation_asset_id"), "wealth_valuation", ["asset_id"], unique=False)
    op.create_table(
        "wealth_loan",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("lender", sa.String(length=120), nullable=True),
        sa.Column("principal", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("rate_pct", sa.Numeric(precision=7, scale=3), nullable=False),
        sa.Column("tenure_months", sa.Integer(), nullable=False),
        sa.Column("emi", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("outstanding", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("outstanding_as_of", sa.Date(), nullable=True),
        sa.Column("floating", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        _created(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "wealth_goal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("target_inr", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("priority", sa.String(length=8), nullable=False),
        sa.Column("inflation_pct", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("current_inr", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("monthly_sip", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("step_up_pct", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("linked_asset_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("portfolio_pct", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("equity_pct", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("gold_pct", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("in_cover", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        _created(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "wealth_policy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("cover_inr", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("premium_inr", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("employer", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        _created(),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("wealth_policy")
    op.drop_table("wealth_goal")
    op.drop_table("wealth_loan")
    op.drop_index(op.f("ix_wealth_valuation_asset_id"), table_name="wealth_valuation")
    op.drop_table("wealth_valuation")
    op.drop_table("wealth_asset")

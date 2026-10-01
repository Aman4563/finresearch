"""Decision journal for every buy and sell (behavioural guardrails): the trade_note table

One new table; nothing existing changes (the IPO `decision` table stays as it is). Personal data: local only, never
sent to an LLM.

Revision ID: d8b4f2a6c1e9
Revises: c6e2a4f8b1d3
Create Date: 2026-10-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8b4f2a6c1e9"
down_revision: str | Sequence[str] | None = "c6e2a4f8b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_note",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("source", sa.String(length=12), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("asset_type", sa.String(length=10), nullable=False),
        sa.Column("instrument", sa.String(length=60), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=True),
        sa.Column("txn_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trade_day", sa.Date(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("price", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("expected_holding_days", sa.Integer(), nullable=True),
        sa.Column("invalidation", sa.Text(), nullable=True),
        sa.Column("confidence_pct", sa.Integer(), nullable=True),
        sa.Column("review_on", sa.Date(), nullable=True),
        sa.Column("review_alerted_on", sa.Date(), nullable=True),
        sa.Column("checklist", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcome_verdict", sa.String(length=12), nullable=True),
        sa.Column("outcome_notes", sa.Text(), nullable=True),
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_trade_note_status"), "trade_note", ["status"], unique=False)
    op.create_index(op.f("ix_trade_note_holding_id"), "trade_note", ["holding_id"], unique=False)
    op.create_index(op.f("ix_trade_note_review_on"), "trade_note", ["review_on"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_trade_note_review_on"), table_name="trade_note")
    op.drop_index(op.f("ix_trade_note_holding_id"), table_name="trade_note")
    op.drop_index(op.f("ix_trade_note_status"), table_name="trade_note")
    op.drop_table("trade_note")

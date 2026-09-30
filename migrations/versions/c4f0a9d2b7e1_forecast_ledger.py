"""forecast ledger

Revision ID: c4f0a9d2b7e1
Revises: c4f1b9d2e7a3
Create Date: 2026-09-30 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4f0a9d2b7e1"
down_revision: str | Sequence[str] | None = "c4f1b9d2e7a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("asset", sa.String(length=20), nullable=False),
        sa.Column("instrument", sa.String(length=60), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("event_kind", sa.String(length=60), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("horizon", sa.String(length=60), nullable=False),
        sa.Column("resolve_on", sa.Date(), nullable=False),
        sa.Column("probability", sa.Float(), nullable=True),
        sa.Column("interval_low", sa.Float(), nullable=True),
        sa.Column("interval_high", sa.Float(), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("method", sa.String(length=200), nullable=False),
        sa.Column("validation_status", sa.String(length=20), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_value", sa.Float(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_run.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(op.f("ix_forecast_asset"), "forecast", ["asset"], unique=False)
    op.create_index(op.f("ix_forecast_instrument"), "forecast", ["instrument"], unique=False)
    op.create_index(op.f("ix_forecast_resolve_on"), "forecast", ["resolve_on"], unique=False)
    op.create_index(op.f("ix_forecast_run_id"), "forecast", ["run_id"], unique=False)
    op.create_index(op.f("ix_forecast_status"), "forecast", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_forecast_status"), table_name="forecast")
    op.drop_index(op.f("ix_forecast_run_id"), table_name="forecast")
    op.drop_index(op.f("ix_forecast_resolve_on"), table_name="forecast")
    op.drop_index(op.f("ix_forecast_instrument"), table_name="forecast")
    op.drop_index(op.f("ix_forecast_asset"), table_name="forecast")
    op.drop_table("forecast")

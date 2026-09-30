"""Broker connections (read-only sync) and their sync log

Two new tables; nothing existing changes. Credentials stay in this local database (masked by the API, redacted from
errors, never sent to an LLM).

Revision ID: b3e9d1f5a7c2
Revises: a8d3c5e7f1b4
Create Date: 2026-10-01 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3e9d1f5a7c2"
down_revision: str | Sequence[str] | None = "a8d3c5e7f1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_connection",
        sa.Column("key", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auto_sync", sa.Boolean(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_day", sa.Date(), nullable=True),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "broker_sync_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=20), nullable=False),
        sa.Column("trigger", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_broker_sync_log_key"), "broker_sync_log", ["key"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_broker_sync_log_key"), table_name="broker_sync_log")
    op.drop_table("broker_sync_log")
    op.drop_table("broker_connection")

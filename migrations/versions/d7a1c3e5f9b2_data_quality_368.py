"""Data quality and lineage (#368): dq_check, dataset_status, dataset_load

Revision ID: d7a1c3e5f9b2
Revises: 87c2628bb597
Create Date: 2026-10-22 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7a1c3e5f9b2"
down_revision: str | Sequence[str] | None = "87c2628bb597"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dq_check",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("dataset", sa.String(length=30), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("check", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("critical", sa.Boolean(), nullable=False),
        sa.Column("value", postgresql.REAL(), nullable=True),
        sa.Column("threshold", postgresql.REAL(), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset", "day", "check"),
    )
    op.create_index(op.f("ix_dq_check_dataset"), "dq_check", ["dataset"], unique=False)
    op.create_index(op.f("ix_dq_check_day"), "dq_check", ["day"], unique=False)
    op.create_table(
        "dataset_status",
        sa.Column("dataset", sa.String(length=30), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("last_loaded", sa.Date(), nullable=True),
        sa.Column("lag_sessions", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("failed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("dataset"),
    )
    op.create_table(
        "dataset_load",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("dataset", sa.String(length=30), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("url", sa.String(length=200), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("last_modified", sa.String(length=40), nullable=True),
        sa.Column("parser_version", sa.String(length=20), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("prior_sha256", sa.String(length=64), nullable=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dataset_load_dataset_day", "dataset_load", ["dataset", "day"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_dataset_load_dataset_day", table_name="dataset_load")
    op.drop_table("dataset_load")
    op.drop_table("dataset_status")
    op.drop_index(op.f("ix_dq_check_day"), table_name="dq_check")
    op.drop_index(op.f("ix_dq_check_dataset"), table_name="dq_check")
    op.drop_table("dq_check")

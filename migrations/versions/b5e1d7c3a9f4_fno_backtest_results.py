"""F&O backtest results (#307): fno_backtest_run, fno_backtest_result

Revision ID: b5e1d7c3a9f4
Revises: d4f8b2c6e0a9
Create Date: 2026-10-12 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5e1d7c3a9f4"
down_revision: str | Sequence[str] | None = "d4f8b2c6e0a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "fno_backtest_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.String(length=30), nullable=False),
        sa.Column("registration_hash", sa.String(length=64), nullable=False),
        sa.Column("registered_hash", sa.String(length=64), nullable=False),
        sa.Column("oos_evaluated", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("variants_tried", sa.Integer(), nullable=False),
        sa.Column("stock_rule_symbol", sa.String(length=30), nullable=True),
        sa.Column("detail", JSON, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "fno_backtest_result",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("underlying", sa.String(length=30), nullable=True),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("trades", JSON, nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["fno_backtest_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "key"),
    )
    op.create_index("ix_fno_backtest_result_run_id", "fno_backtest_result", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_fno_backtest_result_run_id", table_name="fno_backtest_result")
    op.drop_table("fno_backtest_result")
    op.drop_table("fno_backtest_run")

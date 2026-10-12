"""F&O paper book and exit rules (#308): paper_strategy, paper_leg, fno_exit_rule

Revision ID: d4f8b2c6e0a9
Revises: a7c4e9f2b1d6
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4f8b2c6e0a9"
down_revision: str | Sequence[str] | None = "a7c4e9f2b1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
MONEY = sa.Numeric(precision=14, scale=2)


def upgrade() -> None:
    op.create_table(
        "paper_strategy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("opened_at", TS, nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("slippage_pct", sa.Numeric(precision=6, scale=3), nullable=False),
        sa.Column("brokerage_per_order", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("spot_at_entry", MONEY, nullable=True),
        sa.Column("price_source", sa.String(length=80), nullable=False),
        sa.Column("entry_costs", MONEY, nullable=False),
        sa.Column("entry_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("mtm_gross", MONEY, nullable=True),
        sa.Column("mtm_at", TS, nullable=True),
        sa.Column("mtm_source", sa.String(length=80), nullable=True),
        sa.Column("closed_at", TS, nullable=True),
        sa.Column("close_reason", sa.String(length=20), nullable=True),
        sa.Column("exit_costs", MONEY, nullable=True),
        sa.Column("exit_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("settlement_price", MONEY, nullable=True),
        sa.Column("settlement_source", sa.String(length=200), nullable=True),
        sa.Column("gross_pnl", MONEY, nullable=True),
        sa.Column("net_pnl", MONEY, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_strategy_symbol", "paper_strategy", ["symbol"])
    op.create_index("ix_paper_strategy_status", "paper_strategy", ["status"])
    op.create_table(
        "paper_leg",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=False),
        sa.Column("right", sa.String(length=6), nullable=False),
        sa.Column("strike", MONEY, nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("lots", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("ref_price", MONEY, nullable=False),
        sa.Column("entry_price", MONEY, nullable=False),
        sa.Column("trading_symbol", sa.String(length=60), nullable=True),
        sa.Column("last_price", MONEY, nullable=True),
        sa.Column("last_price_at", TS, nullable=True),
        sa.Column("exit_price", MONEY, nullable=True),
        sa.ForeignKeyConstraint(["strategy_id"], ["paper_strategy.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_leg_strategy_id", "paper_leg", ["strategy_id"])
    op.create_table(
        "fno_exit_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paper_id", sa.Integer(), nullable=True),
        sa.Column("position_key", sa.String(length=60), nullable=True),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("threshold", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("checked_at", TS, nullable=True),
        sa.Column("last_value", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("fired_at", TS, nullable=True),
        sa.Column("alert_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["paper_strategy.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fno_exit_rule_paper_id", "fno_exit_rule", ["paper_id"])
    op.create_index("ix_fno_exit_rule_position_key", "fno_exit_rule", ["position_key"])


def downgrade() -> None:
    op.drop_index("ix_fno_exit_rule_position_key", table_name="fno_exit_rule")
    op.drop_index("ix_fno_exit_rule_paper_id", table_name="fno_exit_rule")
    op.drop_table("fno_exit_rule")
    op.drop_index("ix_paper_leg_strategy_id", table_name="paper_leg")
    op.drop_table("paper_leg")
    op.drop_index("ix_paper_strategy_status", table_name="paper_strategy")
    op.drop_index("ix_paper_strategy_symbol", table_name="paper_strategy")
    op.drop_table("paper_strategy")

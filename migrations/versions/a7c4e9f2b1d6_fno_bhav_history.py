"""F&O history backbone (#306): NSE F&O bhavcopy rows, the backfill manifest, derived IV series, contract calendar

Revision ID: a7c4e9f2b1d6
Revises: a7c3e9b1d5f2
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c4e9f2b1d6"
down_revision: str | Sequence[str] | None = "a7c3e9b1d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    price = lambda n: sa.Column(n, sa.Numeric(precision=14, scale=2), nullable=True)  # noqa: E731
    op.create_table(
        "fno_bhav",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("instrument", sa.String(length=3), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        *(price(n) for n in ("open", "high", "low", "close", "settle")),
        sa.Column("oi", sa.BigInteger(), nullable=True),
        sa.Column("chg_oi", sa.BigInteger(), nullable=True),
        sa.Column("contracts", sa.BigInteger(), nullable=True),
        sa.Column("turnover", sa.Numeric(precision=20, scale=2), nullable=True),
        price("underlying"),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("day", "symbol", "instrument", "expiry", "strike", "option_type"),
    )
    op.create_index("ix_fno_bhav_symbol_day", "fno_bhav", ["symbol", "day"], unique=False)
    op.create_table(
        "fno_bhav_day",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("format", sa.String(length=10), nullable=True),
        sa.Column("url", sa.String(length=200), nullable=True),
        sa.Column("rows_total", sa.Integer(), nullable=True),
        sa.Column("rows_kept", sa.Integer(), nullable=True),
        sa.Column("rows_dead", sa.Integer(), nullable=True),
        sa.Column("stock_symbols", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )
    iv = lambda n: sa.Column(n, sa.Numeric(precision=8, scale=3), nullable=True)  # noqa: E731
    op.create_table(
        "fno_iv_eod",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("near_expiry", sa.Date(), nullable=True),
        price("forward"),
        sa.Column("forward_source", sa.String(length=12), nullable=True),
        sa.Column("atm_strike", sa.Numeric(precision=12, scale=2), nullable=True),
        iv("near_iv"),
        sa.Column("next_expiry", sa.Date(), nullable=True),
        iv("next_iv"),
        iv("iv_30d"),
        sa.Column("iv_30d_method", sa.String(length=12), nullable=True),
        iv("skew_25d"),
        iv("rv_20"),
        price("underlying"),
        sa.Column("rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "day"),
    )
    op.create_index(op.f("ix_fno_iv_eod_symbol"), "fno_iv_eod", ["symbol"], unique=False)
    op.create_table(
        "fno_contract",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("underlying_kind", sa.String(length=5), nullable=False),
        sa.Column("kind", sa.String(length=7), nullable=False),
        sa.Column("first_seen", sa.Date(), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("lot_source", sa.String(length=16), nullable=True),
        sa.Column("lot_changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "expiry"),
    )
    op.create_index(op.f("ix_fno_contract_symbol"), "fno_contract", ["symbol"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_fno_contract_symbol"), table_name="fno_contract")
    op.drop_table("fno_contract")
    op.drop_index(op.f("ix_fno_iv_eod_symbol"), table_name="fno_iv_eod")
    op.drop_table("fno_iv_eod")
    op.drop_table("fno_bhav_day")
    op.drop_index("ix_fno_bhav_symbol_day", table_name="fno_bhav")
    op.drop_table("fno_bhav")

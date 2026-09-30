"""Personal portfolio: imports, holdings, transactions, FIFO lots and disposals (roadmap items 8-9)

Seven new tables (plus a daily value snapshot and settings such as target allocation); nothing existing changes. Holdings are one instrument in one account/folio (FIFO runs per holding);
lots and disposals are derived from the transactions and rebuilt by finresearch.portfolio.lots. No identity data
(PAN, name, contact details) and never the CAS password.

Revision ID: a8d3c5e7f1b4
Revises: a9c3e1f7b5d2
Create Date: 2026-09-30 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8d3c5e7f1b4"
down_revision: str | Sequence[str] | None = "a9c3e1f7b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_holding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ikey", sa.String(length=60), nullable=False),
        sa.Column("account", sa.String(length=80), nullable=False),
        sa.Column("asset_type", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("nse_symbol", sa.String(length=30), nullable=True),
        sa.Column("bse_code", sa.String(length=20), nullable=True),
        sa.Column("scheme_code", sa.String(length=20), nullable=True),
        sa.Column("category", sa.String(length=120), nullable=True),
        sa.Column("sector", sa.String(length=120), nullable=True),
        sa.Column("tax_class", sa.String(length=20), nullable=True),
        sa.Column("fmv_2018", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ikey", "account"),
    )
    op.create_index(op.f("ix_portfolio_holding_isin"), "portfolio_holding", ["isin"], unique=False)
    op.create_table(
        "portfolio_import",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("filename", sa.String(length=300), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("saved_path", sa.String(length=600), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256"),
    )
    op.create_table(
        "portfolio_txn",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("price", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("charges", sa.Numeric(precision=16, scale=4), nullable=False),
        sa.Column("stt_paid", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["holding_id"], ["portfolio_holding.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["import_id"], ["portfolio_import.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(op.f("ix_portfolio_txn_day"), "portfolio_txn", ["day"], unique=False)
    op.create_index(op.f("ix_portfolio_txn_holding_id"), "portfolio_txn", ["holding_id"], unique=False)
    op.create_index(op.f("ix_portfolio_txn_import_id"), "portfolio_txn", ["import_id"], unique=False)
    op.create_table(
        "portfolio_lot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=False),
        sa.Column("txn_id", sa.Integer(), nullable=True),
        sa.Column("acquired", sa.Date(), nullable=True),
        sa.Column("origin", sa.String(length=12), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("open_quantity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("cost_per_unit", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("stt_paid", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["holding_id"], ["portfolio_holding.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["txn_id"], ["portfolio_txn.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_portfolio_lot_holding_id"), "portfolio_lot", ["holding_id"], unique=False)
    op.create_table(
        "portfolio_disposal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=False),
        sa.Column("txn_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("acquired", sa.Date(), nullable=True),
        sa.Column("sold", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("cost", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("proceeds", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("stt_paid", sa.Boolean(), nullable=False),
        sa.Column("origin", sa.String(length=12), nullable=False),
        sa.ForeignKeyConstraint(["holding_id"], ["portfolio_holding.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lot_id"], ["portfolio_lot.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["txn_id"], ["portfolio_txn.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_portfolio_disposal_holding_id"), "portfolio_disposal", ["holding_id"], unique=False
    )
    op.create_index(op.f("ix_portfolio_disposal_txn_id"), "portfolio_disposal", ["txn_id"], unique=False)
    op.create_table(
        "portfolio_snapshot",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("invested", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("by_asset", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )
    op.create_table(
        "portfolio_setting",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("portfolio_setting")
    op.drop_table("portfolio_snapshot")
    op.drop_index(op.f("ix_portfolio_disposal_txn_id"), table_name="portfolio_disposal")
    op.drop_index(op.f("ix_portfolio_disposal_holding_id"), table_name="portfolio_disposal")
    op.drop_table("portfolio_disposal")
    op.drop_index(op.f("ix_portfolio_lot_holding_id"), table_name="portfolio_lot")
    op.drop_table("portfolio_lot")
    op.drop_index(op.f("ix_portfolio_txn_import_id"), table_name="portfolio_txn")
    op.drop_index(op.f("ix_portfolio_txn_holding_id"), table_name="portfolio_txn")
    op.drop_index(op.f("ix_portfolio_txn_day"), table_name="portfolio_txn")
    op.drop_table("portfolio_txn")
    op.drop_table("portfolio_import")
    op.drop_index(op.f("ix_portfolio_holding_isin"), table_name="portfolio_holding")
    op.drop_table("portfolio_holding")

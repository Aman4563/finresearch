"""ipo history and subscription archive slots

Revision ID: c3f1a9d2b7e4
Revises: c4f0a9d2b7e1
Create Date: 2026-09-30 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3f1a9d2b7e4"
down_revision: str | Sequence[str] | None = "c4f0a9d2b7e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMES = ("qib", "nii", "bnii", "snii", "retail", "employee", "total")
PRICES = ("open", "high", "low", "close", "vwap", "prev_close")
NUM = {
    "money": sa.Numeric(12, 2),
    "cr": sa.Numeric(14, 2),
    "times": sa.Numeric(14, 4),
    "ret": sa.Numeric(10, 6),
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ipo_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("series", sa.String(length=10), nullable=False),
        sa.Column("company", sa.String(length=300), nullable=True),
        sa.Column("ipo_start", sa.Date(), nullable=True),
        sa.Column("ipo_end", sa.Date(), nullable=True),
        sa.Column("listing_date", sa.Date(), nullable=True),
        sa.Column("issue_price", NUM["money"], nullable=True),
        sa.Column("price_low", NUM["money"], nullable=True),
        sa.Column("price_high", NUM["money"], nullable=True),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("issue_size_cr", NUM["cr"], nullable=True),
        sa.Column("public_book_cr", NUM["cr"], nullable=True),
        sa.Column("fresh_cr", NUM["cr"], nullable=True),
        sa.Column("ofs_cr", NUM["cr"], nullable=True),
        sa.Column("ofs_share", sa.Numeric(8, 4), nullable=True),
        *(sa.Column(f"{k}_times", NUM["times"], nullable=True) for k in TIMES),
        sa.Column("subscription_scope", sa.String(length=40), nullable=True),
        sa.Column("subscription_updated", sa.String(length=80), nullable=True),
        *(sa.Column(f"list_{k}", NUM["money"], nullable=True) for k in PRICES),
        sa.Column("return_open", NUM["ret"], nullable=True),
        sa.Column("return_close", NUM["ret"], nullable=True),
        sa.Column("nifty_ret20_close", NUM["ret"], nullable=True),
        sa.Column("nifty_ret20_listing", NUM["ret"], nullable=True),
        sa.Column("ipo_count_90d", sa.Integer(), nullable=True),
        sa.Column("post_2022", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("missing", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "series", "ipo_start"),
    )
    op.create_index(op.f("ix_ipo_history_symbol"), "ipo_history", ["symbol"], unique=False)
    op.create_index(op.f("ix_ipo_history_listing_date"), "ipo_history", ["listing_date"], unique=False)
    op.create_table(
        "subscription_archive_slot",
        sa.Column("slot", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("slot"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("subscription_archive_slot")
    op.drop_index(op.f("ix_ipo_history_listing_date"), table_name="ipo_history")
    op.drop_index(op.f("ix_ipo_history_symbol"), table_name="ipo_history")
    op.drop_table("ipo_history")

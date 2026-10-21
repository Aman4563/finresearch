"""Forward option quotes (#363): option_quote

Revision ID: 01a11aa9e3f1
Revises: c9e4a2f7b1d3
Create Date: 2026-10-21 21:24:06.849843

Prices and strikes in integer paise, depth as flat integer arrays (fnohist.quotes docstring).

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "01a11aa9e3f1"
down_revision: str | Sequence[str] | None = "c9e4a2f7b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "option_quote",
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oi", sa.BigInteger(), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", sa.Integer(), nullable=False),
        sa.Column("bid", sa.Integer(), nullable=True),
        sa.Column("ask", sa.Integer(), nullable=True),
        sa.Column("bid_qty", sa.Integer(), nullable=True),
        sa.Column("ask_qty", sa.Integer(), nullable=True),
        sa.Column("ltp", sa.Integer(), nullable=True),
        sa.Column("underlying_ltp", sa.Integer(), nullable=True),
        sa.Column("iv", sa.REAL(), nullable=True),
        sa.Column("flags", sa.SmallInteger(), nullable=False),
        sa.Column("underlying", sa.String(length=12), nullable=False),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        sa.Column("source", sa.String(length=5), nullable=False),
        sa.Column("depth_bid", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("depth_ask", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.PrimaryKeyConstraint("snapshot_at", "underlying", "expiry", "strike", "option_type"),
    )


def downgrade() -> None:
    op.drop_table("option_quote")

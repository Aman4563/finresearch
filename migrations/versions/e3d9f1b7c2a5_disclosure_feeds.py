"""Exchange disclosures: feed status and disclosure history

`disclosure_feed` holds each feed's last good read and last failure (surveillance lists, F&O ban list, credit
ratings, SEBI orders; per-stock pledge, insider, SAST and deal feeds). `disclosure_record` keeps the disclosed rows
as history (insider trades, SAST, deals, pledge quarters, rating filings, SEBI orders matched to tracked companies),
de-duplicated by `dedupe_key`. Public market data only.

Revision ID: e3d9f1b7c2a5
Revises: d8b4f2a6c1e9
Create Date: 2026-10-01 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3d9f1b7c2a5"
down_revision: str | Sequence[str] | None = "d8b4f2a6c1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "disclosure_feed",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset", sa.String(length=30), nullable=False),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("as_of", sa.String(length=40), nullable=True),
        sa.Column("ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset", "key"),
    )
    op.create_table(
        "disclosure_record",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=True),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("issuer", sa.String(length=7), nullable=True),
        sa.Column("day", sa.Date(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_disclosure_record_lookup", "disclosure_record", ["dataset", "symbol", "day"], unique=False
    )
    op.create_index(op.f("ix_disclosure_record_isin"), "disclosure_record", ["isin"], unique=False)
    op.create_index(op.f("ix_disclosure_record_issuer"), "disclosure_record", ["issuer"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_disclosure_record_issuer"), table_name="disclosure_record")
    op.drop_index(op.f("ix_disclosure_record_isin"), table_name="disclosure_record")
    op.drop_index("ix_disclosure_record_lookup", table_name="disclosure_record")
    op.drop_table("disclosure_record")
    op.drop_table("disclosure_feed")

"""Portfolio AIS: the user's AIS per financial year, stripped to what the AIS check compares

`portfolio_ais` holds one row per financial year: the AIS rows for dividends, interest, sales, purchases and
off-market transfers (no PAN, name, address or account numbers) and the file's sha256. Personal data, local only.

Revision ID: a4c7e2f9b1d3
Revises: e3d9f1b7c2a5
Create Date: 2026-10-05 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4c7e2f9b1d3"
down_revision: str | Sequence[str] | None = "e3d9f1b7c2a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_ais",
        sa.Column("fy", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ignored", sa.Integer(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("fy"),
    )
    op.create_index(op.f("ix_portfolio_ais_sha256"), "portfolio_ais", ["sha256"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_portfolio_ais_sha256"), table_name="portfolio_ais")
    op.drop_table("portfolio_ais")

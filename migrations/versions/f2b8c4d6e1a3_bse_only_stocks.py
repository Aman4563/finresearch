"""BSE-only stocks: company ISIN, watch exchange + BSE scrip code

Company gets `isin` (and an index on the existing `bse_code`); a watch gets `exchange` ("NSE" by default, so every
existing watch stays an NSE watch) and `bse_code`, and `nse_symbol` becomes nullable so a BSE-only stock can be
watched. A check keeps every watch identifiable: an NSE symbol or a BSE scrip code. Existing rows and their monitor
slots are untouched (an NSE watch's slot key is still its NSE symbol).

Revision ID: f2b8c4d6e1a3
Revises: d7a3e5c1f9b2
Create Date: 2026-09-30 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b8c4d6e1a3"
down_revision: str | Sequence[str] | None = "d7a3e5c1f9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("company", sa.Column("isin", sa.String(length=12), nullable=True))
    op.create_index(op.f("ix_company_isin"), "company", ["isin"], unique=False)
    op.create_index(op.f("ix_company_bse_code"), "company", ["bse_code"], unique=False)
    op.add_column("watch", sa.Column("exchange", sa.String(length=10), server_default="NSE", nullable=False))
    op.add_column("watch", sa.Column("bse_code", sa.String(length=20), nullable=True))
    op.alter_column("watch", "nse_symbol", existing_type=sa.String(length=30), nullable=True)
    op.create_check_constraint(
        "ck_watch_has_instrument", "watch", "nse_symbol IS NOT NULL OR bse_code IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint("ck_watch_has_instrument", "watch", type_="check")
    # a BSE-only watch has no NSE symbol and cannot exist in the old schema: drop it (its checks and alerts cascade)
    op.execute("DELETE FROM watch WHERE nse_symbol IS NULL")
    op.alter_column("watch", "nse_symbol", existing_type=sa.String(length=30), nullable=False)
    op.drop_column("watch", "bse_code")
    op.drop_column("watch", "exchange")
    op.drop_index(op.f("ix_company_bse_code"), table_name="company")
    op.drop_index(op.f("ix_company_isin"), table_name="company")
    op.drop_column("company", "isin")

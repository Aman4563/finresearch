"""F&O contract kind "long-dated" (#354): widen fno_contract.kind from 7 to 12 characters

Revision ID: b4d8e2c6a9f1
Revises: 01a11aa9e3f1
Create Date: 2026-10-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d8e2c6a9f1"
down_revision: str | Sequence[str] | None = "01a11aa9e3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("fno_contract", "kind", existing_type=sa.String(length=7), type_=sa.String(length=12),
                    existing_nullable=False)  # fmt: skip


def downgrade() -> None:
    # a "long-dated" row does not fit the old width: it reads "weekly" again, as before #354
    op.execute("UPDATE fno_contract SET kind = 'weekly' WHERE kind = 'long-dated'")
    op.alter_column("fno_contract", "kind", existing_type=sa.String(length=12), type_=sa.String(length=7),
                    existing_nullable=False)  # fmt: skip

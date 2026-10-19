"""F&O direction model evaluations (#327): fno_direction_run

Revision ID: c9e4a2f7b1d3
Revises: a7c3f1d9e2b8
Create Date: 2026-10-19 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9e4a2f7b1d3"
down_revision: str | Sequence[str] | None = "a7c3f1d9e2b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fno_direction_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.String(length=30), nullable=False),
        sa.Column("registration_hash", sa.String(length=64), nullable=False),
        sa.Column("registered_hash", sa.String(length=64), nullable=False),
        sa.Column("oos_evaluated", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("fno_direction_run")

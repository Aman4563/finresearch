"""Evidence grades (#242): web page snapshots and checked citations

`web_snapshot` keeps the text of each page the `fetch_page` MCP tool returned to an agent (and the JSON of exchange
tool responses), so a web quote is checked against the exact text and the citation stores its sha256.
`citation.computation` records a fincalc re-execution for computed figures.

Revision ID: b8e1d4c7a2f6
Revises: a4c7e2f9b1d3
Create Date: 2026-10-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8e1d4c7a2f6"
down_revision: str | Sequence[str] | None = "a4c7e2f9b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_web_snapshot_run_id"), "web_snapshot", ["run_id"], unique=False)
    op.create_index(op.f("ix_web_snapshot_url"), "web_snapshot", ["url"], unique=False)
    op.create_index(op.f("ix_web_snapshot_sha256"), "web_snapshot", ["sha256"], unique=False)
    op.add_column("citation", sa.Column("snapshot_sha256", sa.String(length=64), nullable=True))
    op.add_column(
        "citation", sa.Column("computation", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("citation", "computation")
    op.drop_column("citation", "snapshot_sha256")
    op.drop_index(op.f("ix_web_snapshot_sha256"), table_name="web_snapshot")
    op.drop_index(op.f("ix_web_snapshot_url"), table_name="web_snapshot")
    op.drop_index(op.f("ix_web_snapshot_run_id"), table_name="web_snapshot")
    op.drop_table("web_snapshot")

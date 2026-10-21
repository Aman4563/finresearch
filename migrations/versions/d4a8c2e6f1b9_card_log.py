"""F&O card log (#360): card_log, append-only (a trigger refuses UPDATE and DELETE)

Revision ID: d4a8c2e6f1b9
Revises: b4d8e2c6a9f1
Create Date: 2026-10-21 21:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4a8c2e6f1b9"
down_revision: str | Sequence[str] | None = "b4d8e2c6a9f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# the same statements as db.models.CARD_LOG_APPEND_ONLY (copied: a migration never imports the current models)
APPEND_ONLY = (
    "CREATE OR REPLACE FUNCTION card_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
    "RAISE EXCEPTION USING MESSAGE = 'card_log is append-only: ' || TG_OP || ' refused'; END $$",
    "CREATE TRIGGER card_log_append_only BEFORE UPDATE OR DELETE ON card_log "
    "FOR EACH ROW EXECUTE FUNCTION card_log_append_only()",
)


def upgrade() -> None:
    op.create_table(
        "card_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("engine_sha", sa.String(length=40), nullable=True),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cards", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_card_log_created_at"), "card_log", ["created_at"], unique=False)
    op.create_index(op.f("ix_card_log_symbol"), "card_log", ["symbol"], unique=False)
    for sql in APPEND_ONLY:
        op.execute(sql)


def downgrade() -> None:
    op.drop_index(op.f("ix_card_log_symbol"), table_name="card_log")
    op.drop_index(op.f("ix_card_log_created_at"), table_name="card_log")
    op.drop_table("card_log")  # drops its trigger
    op.execute("DROP FUNCTION IF EXISTS card_log_append_only()")

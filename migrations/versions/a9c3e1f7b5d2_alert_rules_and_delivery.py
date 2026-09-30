"""Rules and alerts for every asset, with phone delivery (roadmap item 10)

alert_rule_state: where each alert rule stands per instrument (fire once until clear, cooldown, change baselines).
alert_eval_slot: one row per evaluation pass, so two monitors never run the same pass.
notification_setting: channel settings (secrets stay in this local database, masked by the API).
alert_delivery: the delivery log, one row per alert and channel, with attempts and redacted errors.

Revision ID: a9c3e1f7b5d2
Revises: f2b8c4d6e1a3
Create Date: 2026-09-30 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9c3e1f7b5d2"
down_revision: str | Sequence[str] | None = "f2b8c4d6e1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_rule_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.String(length=40), nullable=False),
        sa.Column("instrument", sa.String(length=40), nullable=False),
        sa.Column("rule_sig", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("value", sa.Numeric(20, 6), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("baseline", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_count", sa.Integer(), nullable=False),
        sa.Column("suppressed_count", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "instrument"),
    )
    op.create_index(op.f("ix_alert_rule_state_rule_id"), "alert_rule_state", ["rule_id"], unique=False)
    op.create_table(
        "alert_eval_slot",
        sa.Column("slot", sa.String(length=60), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("slot"),
    )
    op.create_table(
        "notification_setting",
        sa.Column("key", sa.String(length=20), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "alert_delivery",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("priority", sa.String(length=10), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("click", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_try_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("held", sa.String(length=100), nullable=True),
        sa.Column("test", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["alert_id"], ["alert.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alert_id", "channel"),
    )
    op.create_index(op.f("ix_alert_delivery_alert_id"), "alert_delivery", ["alert_id"], unique=False)
    op.create_index(op.f("ix_alert_delivery_status"), "alert_delivery", ["status"], unique=False)
    op.create_index(op.f("ix_alert_delivery_next_try_at"), "alert_delivery", ["next_try_at"], unique=False)


def downgrade() -> None:
    op.drop_table("alert_delivery")
    op.drop_table("notification_setting")
    op.drop_table("alert_eval_slot")
    op.drop_index(op.f("ix_alert_rule_state_rule_id"), table_name="alert_rule_state")
    op.drop_table("alert_rule_state")

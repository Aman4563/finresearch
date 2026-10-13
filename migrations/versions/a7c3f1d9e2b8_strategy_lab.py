"""F&O Strategy Lab (#318): lab_strategy, lab_version, lab_run, lab_variant, lab_paper_rule, lab_paper_signal

Revision ID: a7c3f1d9e2b8
Revises: b5e1d7c3a9f4
Create Date: 2026-10-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3f1d9e2b8"
down_revision: str | Sequence[str] | None = "b5e1d7c3a9f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = postgresql.JSONB(astext_type=sa.Text())
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "lab_strategy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("underlying", sa.String(length=30), nullable=False),
        sa.Column("is_start", sa.Date(), nullable=False),
        sa.Column("is_end", sa.Date(), nullable=False),
        sa.Column("oos_start", sa.Date(), nullable=False),
        sa.Column("data_first", sa.Date(), nullable=False),
        sa.Column("data_last", sa.Date(), nullable=False),
        sa.Column("split_rule", sa.String(length=300), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lab_strategy_underlying", "lab_strategy", ["underlying"])
    op.create_table(
        "lab_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("spec", JSON, nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["strategy_id"], ["lab_strategy.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_id", "version"),
    )
    op.create_index("ix_lab_version_strategy_id", "lab_version", ["strategy_id"])
    op.create_index("ix_lab_version_spec_hash", "lab_version", ["spec_hash"])
    op.create_table(
        "lab_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("oos", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("progress_note", sa.String(length=200), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", TZ, nullable=True),
        sa.Column("heartbeat_at", TZ, nullable=True),
        sa.Column("finished_at", TZ, nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("n_trials", sa.Integer(), nullable=True),
        sa.Column("counted", sa.Boolean(), nullable=False),
        sa.Column("result", JSON, nullable=False),
        sa.Column("trades", JSON, nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["lab_version.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lab_run_version_id", "lab_run", ["version_id"])
    op.create_index("ix_lab_run_status", "lab_run", ["status"])
    op.create_table(
        "lab_variant",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("daily_sr", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["version_id"], ["lab_version.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["lab_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spec_hash"),
    )
    op.create_index("ix_lab_variant_version_id", "lab_variant", ["version_id"])
    op.create_table(
        "lab_paper_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("start_after", sa.Date(), nullable=False),
        sa.Column("last_day", sa.Date(), nullable=True),
        sa.Column("stopped_at", TZ, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["version_id"], ["lab_version.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lab_paper_rule_version_id", "lab_paper_rule", ["version_id"])
    op.create_index("ix_lab_paper_rule_status", "lab_paper_rule", ["status"])
    op.create_table(
        "lab_paper_signal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("lots", sa.Integer(), nullable=False),
        sa.Column("lot", sa.Integer(), nullable=False),
        sa.Column("legs", JSON, nullable=False),
        sa.Column("premium", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_loss", sa.Numeric(14, 2), nullable=True),
        sa.Column("margin", sa.Numeric(14, 2), nullable=True),
        sa.Column("iv_pct", sa.Float(), nullable=True),
        sa.Column("paper_id", sa.Integer(), nullable=True),
        sa.Column("exit_reason", sa.String(length=20), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["rule_id"], ["lab_paper_rule.id"]),
        sa.ForeignKeyConstraint(["paper_id"], ["paper_strategy.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "day"),
    )
    op.create_index("ix_lab_paper_signal_rule_id", "lab_paper_signal", ["rule_id"])


def downgrade() -> None:
    op.drop_index("ix_lab_paper_signal_rule_id", table_name="lab_paper_signal")
    op.drop_table("lab_paper_signal")
    op.drop_index("ix_lab_paper_rule_status", table_name="lab_paper_rule")
    op.drop_index("ix_lab_paper_rule_version_id", table_name="lab_paper_rule")
    op.drop_table("lab_paper_rule")
    op.drop_index("ix_lab_variant_version_id", table_name="lab_variant")
    op.drop_table("lab_variant")
    op.drop_index("ix_lab_run_status", table_name="lab_run")
    op.drop_index("ix_lab_run_version_id", table_name="lab_run")
    op.drop_table("lab_run")
    op.drop_index("ix_lab_version_spec_hash", table_name="lab_version")
    op.drop_index("ix_lab_version_strategy_id", table_name="lab_version")
    op.drop_table("lab_version")
    op.drop_index("ix_lab_strategy_underlying", table_name="lab_strategy")
    op.drop_table("lab_strategy")

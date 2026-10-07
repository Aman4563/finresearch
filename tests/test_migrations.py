"""Alembic migrations agree with the models (#247): the test schema is built with create_all (conftest), so nothing
else would catch a model change without a migration, or a migration that does not apply on an empty database.

Runs `alembic upgrade head` on an empty scratch database, compares the result with Base.metadata using alembic's
autogenerate comparison (tables, columns, types, nullability, indexes, unique constraints, foreign keys; server
defaults and CHECK constraints are not compared), then downgrades the latest migration and upgrades it again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent


def _config():
    from alembic.config import Config

    cfg = Config()  # no ini file: env.py would otherwise reset the test run's logging with fileConfig
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def _diff(url: str) -> list:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from finresearch.db.models import Base

    eng = create_engine(url)
    try:
        with eng.connect() as c:
            ctx = MigrationContext.configure(c, opts={"compare_type": True})
            return compare_metadata(ctx, Base.metadata)
    finally:
        eng.dispose()


@pytest.fixture
def scratch_db(db_url, monkeypatch):
    """An empty database; migrations/env.py reads its URL from the settings (FINRESEARCH_DATABASE_URL)."""
    from finresearch import config

    base = make_url(db_url)
    name = base.database.removesuffix("_test") + "_migrate_test"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as e:  # no CREATEDB right (CI runs the same check as a workflow step)
        admin.dispose()
        pytest.skip(f"cannot create a scratch database: {e}")
    url = base.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("FINRESEARCH_DATABASE_URL", url)
    config.get_settings.cache_clear()
    try:
        yield url
    finally:
        config.get_settings.cache_clear()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_upgrade_head_on_an_empty_database_matches_the_models(scratch_db):
    from alembic import command
    from alembic.script import ScriptDirectory

    from finresearch.db.models import Base

    cfg = _config()
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"several alembic heads: {heads}"
    command.upgrade(cfg, "head")
    eng = create_engine(scratch_db)
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == heads[0]
        tables = set(inspect(c).get_table_names()) - {"alembic_version"}
    eng.dispose()
    assert tables == set(Base.metadata.tables)
    assert _diff(scratch_db) == []


def test_latest_migration_downgrades_and_upgrades_again(scratch_db):
    from alembic import command

    cfg = _config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    assert _diff(scratch_db) != []  # the downgrade really removed what the latest migration adds
    command.upgrade(cfg, "head")
    assert _diff(scratch_db) == []

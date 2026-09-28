"""Shared fixtures: a real Postgres+pgvector test database (skipped when unreachable)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture(autouse=True)
def _isolate_data_dirs(tmp_path, monkeypatch):
    """Every test writes runs, state, documents and caches under its own tmp dir, never the real data/ folder.

    (Regression: pipeline tests once overwrote a live run's report because runs_dir still pointed at data/runs.)
    """
    from finresearch import config

    for var, sub in (("FINRESEARCH_RUNS_DIR", "runs"), ("FINRESEARCH_STATE_DIR", "state"),
                     ("FINRESEARCH_DOCS_DIR", "docs")):  # fmt: skip
        monkeypatch.setenv(var, str(tmp_path / "_iso" / sub))
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture(scope="session")
def db_url():
    from finresearch.config import Settings

    url = Settings().test_database_url
    try:
        eng = create_engine(url)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("test database not reachable")
    from finresearch.db.models import Base

    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    eng.dispose()
    return url


@pytest.fixture
def env(db_url, tmp_path, monkeypatch):
    """Point settings + engine at the test DB and a temp docs dir; reset cached singletons."""
    from finresearch import config, db

    monkeypatch.setenv("FINRESEARCH_DATABASE_URL", db_url)
    monkeypatch.setenv("FINRESEARCH_DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setenv("FINRESEARCH_STATE_DIR", str(tmp_path / "state"))
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    yield config.get_settings()
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()

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
                     ("FINRESEARCH_DOCS_DIR", "docs"), ("FINRESEARCH_REPORTS_DIR", "reports"), ("FINRESEARCH_PORTFOLIO_DIR", "portfolio")):  # fmt: skip
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

    # reset the whole schema: tables created by other branches' models would block a metadata drop_all
    if not eng.url.database or not eng.url.database.endswith("_test"):
        pytest.exit(f"refusing to reset {eng.url.database!r}: the test database name must end in _test")
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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


def minimal_pdf(pages: list[list[str]]) -> bytes:
    """A tiny valid PDF: one Helvetica text line per entry; an empty list = page with no text layer."""
    objs: list[bytes] = []
    n_pages = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    font_id = 3 + 2 * n_pages
    for i, lines in enumerate(pages):
        content_id = 4 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        ops = ["BT", "/F1 11 Tf", "14 TL", "60 740 Td"]
        for ln in lines:
            ops.append("(" + ln.replace("(", r"\(").replace(")", r"\)") + ") Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode() if lines else b""
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for k, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{k} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)

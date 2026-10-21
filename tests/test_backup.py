"""scripts/backup.sh: dump the (test) database, rotate old dumps, and restore a dump into a scratch database."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup.sh"
TOOLS = ("pg_dump", "pg_restore")
pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in TOOLS), reason="pg_dump/pg_restore not installed"
)


def _run_backup(url: str, out: Path, docs: Path, keep: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "FINRESEARCH_DATABASE_URL": url, "BACKUP_DIR": str(out), "DOCS_DIR": str(docs),
           "KEEP": str(keep)}  # fmt: skip
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=300)


def test_backup_rotates_and_restores_into_a_scratch_database(env, db_url, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Company, Forecast
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        get_or_create_company(s, "backup-" + tmp_path.name[-8:], "Backup Co")
        companies, forecasts = s.query(Company).count(), s.query(Forecast).count()
    docs = tmp_path / "docs"
    (docs / "abc").mkdir(parents=True)
    (docs / "abc" / "rhp.txt").write_text("hello")
    out = tmp_path / "backups"
    first = _run_backup(db_url, out, docs, keep=2)
    if first.returncode != 0 and "version mismatch" in first.stderr:
        pytest.skip(f"pg_dump older than the server: {first.stderr.strip()[:200]}")
    assert first.returncode == 0, first.stderr
    for _ in range(2):
        assert _run_backup(db_url, out, docs, keep=2).returncode == 0
    dumps = sorted(out.glob("finresearch-*.dump"))
    assert len(dumps) == 2 and not list(out.glob("*.partial"))  # rotated to the newest two
    manifest = dumps[-1].with_name(dumps[-1].name.replace(".dump", ".docs-manifest.tsv"))
    assert manifest.read_text().strip() == "5\t./abc/rhp.txt"

    base = make_url(db_url)
    scratch = base.database.removesuffix("_test") + "_restore_test"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
            c.execute(text(f'CREATE DATABASE "{scratch}"'))
    except Exception as e:  # no CREATEDB right: the dump itself was still verified by pg_restore --list
        pytest.skip(f"cannot create a scratch database: {e}")
    target = base.set(database=scratch, drivername="postgresql")
    try:
        r = subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
                            f"--dbname={target.render_as_string(hide_password=False)}", str(dumps[-1])],
                           capture_output=True, text=True, timeout=300)  # fmt: skip
        assert r.returncode == 0, r.stderr
        eng = create_engine(base.set(database=scratch))
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM company")).scalar() == companies
            assert c.execute(text("SELECT count(*) FROM forecast")).scalar() == forecasts
            assert c.execute(text("SELECT 1 FROM company WHERE name = 'Backup Co'")).scalar() == 1
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()


def test_backup_refuses_a_bad_keep(tmp_path):
    r = _run_backup("postgresql://localhost/none", tmp_path, tmp_path, keep=0)
    assert r.returncode == 2 and "KEEP" in r.stderr


def test_backup_also_pushes_an_encrypted_off_machine_copy_when_configured(env, db_url, tmp_path, monkeypatch):
    from finresearch import config
    from finresearch.backups import offsite

    monkeypatch.setenv("FINRESEARCH_SECRETS_BACKEND", "file")  # shared with the script: a 0600 file in tmp state
    config.get_settings.cache_clear()
    offsite.init_key()
    dest = tmp_path / "offsite"
    dest.mkdir()
    monkeypatch.setenv("FINRESEARCH_BACKUP_OFFSITE_DIR", str(dest))
    r = _run_backup(db_url, tmp_path / "backups", tmp_path / "docs", keep=2)
    if r.returncode != 0 and "version mismatch" in r.stderr:
        pytest.skip("pg_dump older than the server")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(list((dest / "db").glob("*.enc"))) == 1 and len(list((dest / "tables").glob("*.enc"))) == 1
    assert offsite.status(dest)["tables"][0]["ok"] is True


def test_a_failed_off_machine_copy_fails_the_job_but_keeps_the_local_dump(env, db_url, tmp_path, monkeypatch):
    monkeypatch.setenv("FINRESEARCH_BACKUP_OFFSITE_DIR", str(tmp_path / "not-mounted"))
    out = tmp_path / "backups"
    r = _run_backup(db_url, out, tmp_path / "docs", keep=2)
    if "version mismatch" in r.stderr:
        pytest.skip("pg_dump older than the server")
    assert r.returncode == 4 and "off-machine" in r.stderr
    assert len(list(out.glob("finresearch-*.dump"))) == 1
    assert not (tmp_path / "not-mounted").exists()

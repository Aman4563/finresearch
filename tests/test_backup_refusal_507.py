"""#507: on 12 nights the backup logged "REFUSED: plaintext secrets" while the real cause was an unreachable
database. `finresearch secrets check` and scripts/backup.sh now tell the two apart, and the script records each
run's outcome for the data-health card (dq.misc.backup_check). The script is run with a fake `finresearch` on PATH:
no database, no dump, no Keychain."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup.sh"


def _fake_bin(tmp_path: Path, check_exit: int) -> Path:
    """A folder with a `finresearch` that answers `secrets check` with `check_exit`, and stand-in pg tools (the
    script only looks for them before the check; they are never run on these paths)."""
    d = tmp_path / "bin"
    d.mkdir()
    (d / "finresearch").write_text(
        f'#!/bin/sh\necho "fake finresearch $*" >> "{tmp_path}/calls"\nexit {check_exit}\n'
    )
    for tool in ("pg_dump", "pg_restore"):
        (d / tool).write_text(f'#!/bin/sh\necho "{tool} must not run" >&2\nexit 99\n')
    for f in d.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return d


def _run(tmp_path: Path, check_exit: int) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    fake = _fake_bin(tmp_path, check_exit)
    state = tmp_path / "state"
    env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "FINRESEARCH_CLI": "finresearch",
           "FINRESEARCH_DATABASE_URL": "postgresql+psycopg://localhost:1/none", "FINRESEARCH_STATE_DIR": str(state),
           "BACKUP_DIR": str(tmp_path / "backups"), "DOCS_DIR": str(tmp_path / "docs"), "KEEP": "2"}  # fmt: skip
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    status = dict(ln.split("=", 1) for ln in (state / "backup_status").read_text().splitlines() if "=" in ln)
    return r, status


def test_plaintext_secrets_still_refuse_with_exit_3(tmp_path):
    r, status = _run(tmp_path, check_exit=1)
    assert r.returncode == 3 and "REFUSED: plaintext secrets" in r.stderr
    assert (tmp_path / "calls").read_text().strip() == "fake finresearch secrets check"
    assert status["outcome"] == "refused_plaintext_secrets" and status["exit"] == "3"
    assert not list((tmp_path / "backups").glob("*.dump"))


def test_an_unreachable_database_is_not_reported_as_plaintext_secrets(tmp_path):
    r, status = _run(tmp_path, check_exit=3)
    assert r.returncode == 5, r.stderr  # before #507: exit 3 and "REFUSED: plaintext secrets"
    assert "cannot reach the database" in r.stderr and "plaintext" not in r.stderr.replace(
        "not the plaintext", ""
    )
    assert status["outcome"] == "database_unreachable" and status["exit"] == "5"
    assert "last_ok_at" not in status  # no good backup was ever recorded here: unknown, not a date


def test_a_check_that_could_not_run_is_neither(tmp_path):
    r, status = _run(tmp_path, check_exit=7)
    assert r.returncode == 6, r.stderr
    assert "could not run" in r.stderr and "REFUSED" not in r.stderr
    assert status["outcome"] == "secrets_check_failed"


def test_a_failed_run_keeps_the_last_good_backup_on_record(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "backup_status").write_text(
        "at=2026-11-11T21:00:07Z\noutcome=ok\nexit=0\nlast_ok_at=2026-11-11T21:00:07Z\n"
        "last_ok_dump=finresearch-20261111T210007Z-123.dump\n"
    )
    r, status = _run(tmp_path, check_exit=3)
    assert r.returncode == 5
    assert status["outcome"] == "database_unreachable" and status["at"] != "2026-11-11T21:00:07Z"
    assert status["last_ok_at"] == "2026-11-11T21:00:07Z"
    assert status["last_ok_dump"] == "finresearch-20261111T210007Z-123.dump"


# ---------------------------------------------------------------------------------------------- the command
@pytest.fixture
def dead_database(monkeypatch):
    """The configured database is a closed local port (nothing listens on 1); the caches are reset on both sides."""
    from finresearch import config, db

    monkeypatch.setenv("FINRESEARCH_DATABASE_URL", "postgresql+psycopg://127.0.0.1:1/none?connect_timeout=2")
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    yield
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()


def test_secrets_check_says_the_database_is_unreachable_with_its_own_exit_code(dead_database):
    from finresearch.cli import app

    r = CliRunner().invoke(app, ["secrets", "check"])
    assert r.exit_code == 3, r.output  # before #507: an uncaught error, exit 1, the same as "plaintext found"
    assert "cannot reach the database" in r.output and "nothing was checked" in r.output
    assert "plaintext secrets in the database" not in r.output and "no plaintext secrets" not in r.output


def test_secrets_check_that_crashes_is_not_exit_1(env, monkeypatch):
    from finresearch import secrets_migrate
    from finresearch.cli import app

    def boom(_s):
        raise RuntimeError("scan broke")

    monkeypatch.setattr(secrets_migrate, "scan", boom)
    r = CliRunner().invoke(app, ["secrets", "check"])
    assert r.exit_code == 4 and "could not be completed" in r.output and "RuntimeError" in r.output


@pytest.mark.skipif(
    not all(shutil.which(t) for t in ("pg_dump", "pg_restore")), reason="pg_dump/pg_restore missing"
)
def test_a_good_run_records_its_dump_and_a_later_failure_keeps_it(env, db_url, tmp_path):
    state, out = tmp_path / "state", tmp_path / "backups"
    base = {**os.environ, "FINRESEARCH_DATABASE_URL": db_url, "FINRESEARCH_STATE_DIR": str(state),
            "BACKUP_DIR": str(out), "DOCS_DIR": str(tmp_path / "docs"), "KEEP": "2"}  # fmt: skip
    r = subprocess.run(["bash", str(SCRIPT)], env=base, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 and "version mismatch" in r.stderr:
        pytest.skip(f"pg_dump older than the server: {r.stderr.strip()[:200]}")
    assert r.returncode == 0, r.stderr
    good = dict(ln.split("=", 1) for ln in (state / "backup_status").read_text().splitlines())
    (dump,) = out.glob("finresearch-*.dump")
    assert good["outcome"] == "ok" and good["exit"] == "0"
    assert good["last_ok_dump"] == dump.name and good["last_ok_at"] == good["at"]
    # the next night the database is down: the failure is recorded, the good dump stays on record
    fake = _fake_bin(tmp_path, check_exit=3)
    env2 = {**base, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "FINRESEARCH_CLI": "finresearch"}
    assert (
        subprocess.run(
            ["bash", str(SCRIPT)], env=env2, capture_output=True, text=True, timeout=120
        ).returncode
        == 5
    )
    bad = dict(ln.split("=", 1) for ln in (state / "backup_status").read_text().splitlines())
    assert bad["outcome"] == "database_unreachable"
    assert (bad["last_ok_at"], bad["last_ok_dump"]) == (good["last_ok_at"], dump.name)

"""`finresearch backup ...` (#375): exit codes the launchd jobs and the owner rely on. Destination: a tmp dir."""

from __future__ import annotations

import shutil

import pytest
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from finresearch.cli import app


def _run(*args: str, dest=None, monkeypatch=None):
    from finresearch import config

    if monkeypatch is not None and dest is not None:
        monkeypatch.setenv("FINRESEARCH_BACKUP_OFFSITE_DIR", str(dest))
        config.get_settings.cache_clear()
    return CliRunner().invoke(app, ["backup", *args])


def test_status_without_a_destination_fails(env, monkeypatch):
    monkeypatch.delenv("FINRESEARCH_BACKUP_OFFSITE_DIR", raising=False)
    r = _run("status")
    assert r.exit_code == 1 and "FINRESEARCH_BACKUP_OFFSITE_DIR" in r.output


def test_init_key_once_then_refuses(env):
    assert _run("init-key").exit_code == 0
    r = _run("init-key")
    assert r.exit_code == 1 and "already exists" in r.output


def test_offsite_without_a_key_fails(env, tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    r = _run("offsite", dest=dest, monkeypatch=monkeypatch)
    assert r.exit_code == 1 and "init-key" in r.output
    assert list(dest.rglob("*.enc")) == []


@pytest.mark.skipif(
    not all(shutil.which(t) for t in ("pg_dump", "pg_restore")), reason="no pg_dump/pg_restore"
)
def test_offsite_status_drill_round_trip(env, db_url, tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    assert _run("init-key").exit_code == 0
    r = _run("offsite", dest=dest, monkeypatch=monkeypatch)
    assert r.exit_code == 0, r.output
    r = _run("status")
    assert r.exit_code == 1 and "no restore drill" in r.output  # copies are fresh, but no drill yet
    scratch = make_url(db_url).database.removesuffix("_test") + "_clidrill_test"
    r = _run("drill", "--scratch", scratch)
    if "cannot create the scratch database" in r.output:
        pytest.skip(r.output)
    assert r.exit_code == 0, r.output
    r = _run("status")
    assert r.exit_code == 0, r.output
    r = _run("drill", "--scratch", "finresearch")
    assert r.exit_code == 2 and "_test" in r.output

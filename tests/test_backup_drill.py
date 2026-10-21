"""Off-machine copies against the test database and the restore drill into a scratch *_test database (#375).

The destination is a tmp directory; the key lives in the file backend under the test's state dir (a *_test database
never reaches the Keychain)."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from finresearch.backups import drill, offsite

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("pg_dump", "pg_restore")), reason="pg_dump/pg_restore not installed"
)


def _scratch(db_url: str) -> str:
    return make_url(db_url).database.removesuffix("_test") + "_drill_test"


def _seed(n: int, tag: str) -> int:
    """Add n synthetic snapshots; returns the table's row count (rows persist across tests in one session)."""
    from finresearch.db import session_scope
    from finresearch.db.models import SubscriptionSnapshotRow

    with session_scope() as s:
        for i in range(n):
            s.add(SubscriptionSnapshotRow(nse_symbol="FAKE" + tag[-8:].upper(), source="nse_combined",
                                          as_of=datetime(2026, 9, 28, 10, i, tzinfo=UTC)))  # fmt: skip
    with session_scope() as s:
        return s.query(SubscriptionSnapshotRow).count()


def _gone(db_url: str, name: str) -> bool:
    eng = create_engine(make_url(db_url).set(database="postgres"))
    with eng.connect() as c:
        n = c.execute(text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": name}).scalar()
    eng.dispose()
    return n == 0


def test_dump_counts_rows_in_the_dumps_own_snapshot(env, db_url, tmp_path):
    from finresearch.backups.dump import dump_with_counts

    rows = _seed(2, tmp_path.name)
    out = tmp_path / "t.dump"
    counts = dump_with_counts(db_url, out, tables=offsite.FORWARD_ONLY)
    assert set(counts) == set(offsite.FORWARD_ONLY) and counts["subscription_snapshot"] == rows >= 2
    listing = subprocess.run(
        ["pg_restore", "--list", str(out)], capture_output=True, text=True, check=True
    ).stdout
    assert "TABLE DATA public subscription_snapshot" in listing and "TABLE DATA public company" not in listing


def test_drill_refuses_any_target_that_is_not_a_scratch_test_database(env, db_url, tmp_path):
    for bad in ("finresearch", "finresearch_restored", make_url(db_url).database):
        with pytest.raises(drill.DrillRefused):
            drill.check_target(bad)
    eng = create_engine(make_url(db_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = _scratch(db_url).replace("_drill_test", "_exists_test")
    try:
        with eng.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as e:
        pytest.skip(f"cannot create a scratch database: {e}")
    try:
        with pytest.raises(drill.DrillRefused, match="already exists"):
            drill.check_target(name)
    finally:
        with eng.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        eng.dispose()


def test_backup_then_drill_restores_matching_row_counts(env, db_url, tmp_path):
    rows = _seed(3, tmp_path.name)
    dest = tmp_path / "offsite"
    dest.mkdir()
    offsite.init_key()
    made = offsite.backup_now(db_url, dest, kinds=("tables", "db"))
    assert set(made) == {"tables", "db"}
    assert offsite.manifest_of(made["tables"])["counts"]["subscription_snapshot"] == rows
    assert all(t["ok"] for t in offsite.status(dest)["tables"])

    scratch = _scratch(db_url)
    for kind in ("tables", "db"):
        res = drill.run(dest, kind=kind, scratch=scratch)
        if res.get("skipped"):
            pytest.skip(res["skipped"])
        assert res["ok"] is True, res
        assert res["restored"]["subscription_snapshot"] == res["expected"]["subscription_snapshot"] == rows
        assert _gone(db_url, scratch)
    log = [json.loads(x) for x in (dest / offsite.DRILL_LOG).read_text().splitlines()]
    assert [d["kind"] for d in log] == ["tables", "db"] and all(d["ok"] for d in log)
    assert offsite.status(dest)["ok"] is True


def test_drill_reports_a_row_count_mismatch(env, db_url, tmp_path):
    rows = _seed(1, tmp_path.name)
    dest = tmp_path / "offsite"
    dest.mkdir()
    offsite.init_key()
    copy = offsite.backup_now(db_url, dest, kinds=("tables",))["tables"]
    man = copy.with_suffix(".json")
    m = json.loads(man.read_text())
    m["counts"]["subscription_snapshot"] = rows + 1  # the manifest claims one row more than the dump holds
    man.write_text(json.dumps(m))
    res = drill.run(dest, kind="tables", scratch=_scratch(db_url))
    if res.get("skipped"):
        pytest.skip(res["skipped"])
    assert res["ok"] is False and res["mismatched"] == ["subscription_snapshot"]
    assert offsite.status(dest)["drill"]["ok"] is False  # a failed drill never counts as the quarter's drill
    assert _gone(db_url, _scratch(db_url))

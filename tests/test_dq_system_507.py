"""#507: two checks the data-health pass makes on the machine itself, so the next brief and the Data health card say
it plainly: how old the newest good backup is (from the outcome scripts/backup.sh records on every run), and whether
a new database session can load pgvector (a `brew upgrade` on 23-Nov-2026 removed its Postgres 17 build and nothing
reported it). Unknown is never ok."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text

from finresearch.dq import misc

NOW = datetime(2026, 11, 23, 2, 30, tzinfo=UTC)  # Monday 08:00 IST: the morning pass before the brief


def _status(d: Path, **kv: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "backup_status").write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
    return d


# ---------------------------------------------------------------------------------------------- the backup
def test_no_recorded_run_is_unknown_never_ok(tmp_path):
    c = misc.backup_check(tmp_path, NOW)
    assert (c.dataset, c.check, c.status, c.value, c.critical) == (
        "backup",
        "last_good_backup",
        "unknown",
        None,
        True,
    )
    assert "no backup run is recorded" in c.note


def test_last_nights_good_backup_is_ok_with_its_age(tmp_path):
    # 21:00 UTC on Sunday is 02:30 IST on Monday: five and a half hours before this pass
    d = _status(tmp_path, at="2026-11-22T21:00:00Z", outcome="ok", exit="0", last_ok_at="2026-11-22T21:00:00Z",
                last_ok_dump="finresearch-20261122T210000Z-1.dump")  # fmt: skip
    c = misc.backup_check(d, NOW)
    assert (c.status, c.value, c.threshold) == ("ok", 5.5, 36)
    assert c.note == "newest good backup 5.5 h old (22-Nov-2026 21:00 UTC)"


def test_twelve_failed_nights_read_as_what_happened(tmp_path):
    # the outage: the last good dump on 11-Nov, and last night's run could not reach the database
    d = _status(tmp_path, at="2026-11-22T21:00:05Z", outcome="database_unreachable", exit="5",
                last_ok_at="2026-11-11T21:00:07Z", last_ok_dump="finresearch-20261111T210007Z-9.dump")  # fmt: skip
    c = misc.backup_check(d, NOW)
    # 11-Nov 21:00:07 to 23-Nov 02:30:00 UTC = 11 days 5 h 29 min 53 s = 269.5 h = 11.2 days
    assert (c.status, c.value) == ("fail", 269.5)
    assert c.note == ("the last run (22-Nov-2026 21:00 UTC) could not reach the database; newest good backup "
                      "11.2 days old (11-Nov-2026 21:00 UTC)")  # fmt: skip


def test_a_refused_run_fails_even_when_yesterdays_dump_is_fresh(tmp_path):
    d = _status(tmp_path, at="2026-11-22T21:00:00Z", outcome="refused_plaintext_secrets", exit="3",
                last_ok_at="2026-11-21T21:00:00Z", last_ok_dump="x.dump")  # fmt: skip
    c = misc.backup_check(d, NOW)  # 29.5 h old: inside the 36 h allowance, but last night's run was refused
    assert (c.status, c.value) == ("fail", 29.5)
    assert "was REFUSED: plaintext secrets are still in the database" in c.note


def test_a_job_that_stopped_running_fails_on_age(tmp_path):
    d = _status(tmp_path, at="2026-11-20T21:00:00Z", outcome="ok", exit="0", last_ok_at="2026-11-20T21:00:00Z",
                last_ok_dump="x.dump")  # fmt: skip
    c = misc.backup_check(d, NOW)  # 53.5 h: two nights without a run
    assert (c.status, c.value) == ("fail", 53.5)
    assert c.note.startswith("no backup has run since: newest good backup 2.2 days old")


def test_a_failed_run_with_no_good_backup_on_record_and_an_offsite_failure(tmp_path):
    d = _status(tmp_path / "a", at="2026-11-22T21:00:00Z", outcome="failed", exit="1")
    c = misc.backup_check(d, NOW)
    assert (c.status, c.value) == ("fail", None) and c.note.endswith("no good backup is on record")
    d = _status(tmp_path / "b", at="2026-11-22T21:00:00Z", outcome="offsite_failed", exit="4",
                last_ok_at="2026-11-22T21:00:00Z", last_ok_dump="x.dump")  # fmt: skip
    c = misc.backup_check(d, NOW)  # the local dump is good: a warning, not a failure
    assert c.status == "warn" and "off-machine copy failed" in c.note


def test_a_status_file_that_cannot_be_read_is_unknown(tmp_path):
    (tmp_path / "backup_status").write_text("not a status file\n")
    assert misc.backup_check(tmp_path, NOW).status == "unknown"
    d = _status(tmp_path / "b", at="yesterday", outcome="ok", exit="0", last_ok_at="yesterday")
    assert misc.backup_check(d, NOW).status == "unknown"


# ---------------------------------------------------------------------------------------------- pgvector
def test_pgvector_loads_on_the_test_database(env):
    c = misc.vector_check()
    assert (c.dataset, c.check, c.status, c.critical) == ("pgvector", "extension_loadable", "ok", True)
    assert misc.vector_loadable() is True


def test_a_missing_library_is_a_plain_failure(monkeypatch):
    class Undefined(Exception):
        sqlstate = "58P01"

    def gone(_sql: str) -> None:
        err = Exception("wrapped")
        err.orig = Undefined('could not access file "$libdir/vector": No such file or directory')
        raise err

    monkeypatch.setattr(misc, "_fresh_session", gone)
    c = misc.vector_check()
    assert c.status == "fail" and c.critical
    assert c.note.startswith('could not access file "$libdir/vector": No such file or directory')
    assert "ENGINEERING_HANDOFF" in c.note and misc.vector_loadable() is False


def test_a_real_missing_library_and_a_missing_type_fail_and_other_errors_are_unknown(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        superuser = s.scalar(text("select rolsuper from pg_roles where rolname = current_user"))
        if superuser:  # LOAD of a file that is not there raises the incident's own error (SQLSTATE 58P01)
            c = misc.vector_check("LOAD '$libdir/vector_missing_507'")
            assert c.status == "fail" and "could not access file" in c.note
        c = misc.vector_check("select '[1]'::vector_missing_507")
        assert c.status == "fail" and "not installed in this database" in c.note
        c = misc.vector_check("select 1/0")
        assert c.status == "unknown" and "could not be checked" in c.note
        assert s.scalar(select(1)) == 1  # the probe has its own connection: this session is untouched


# ---------------------------------------------------------------------------------------------- where it shows
def test_the_pass_stores_both_and_the_brief_names_a_failed_backup(env):
    from finresearch.db import session_scope
    from finresearch.dq import report, run

    _status(Path(env.state_dir), at="2026-11-22T21:00:05Z", outcome="database_unreachable", exit="5",
            last_ok_at="2026-11-11T21:00:07Z", last_ok_dump="x.dump")  # fmt: skip
    with session_scope() as s:
        s.execute(text("TRUNCATE dq_check, dataset_status"))
        out = run.run_all(s, NOW, holidays=set(), free_bytes=lambda _p: 50 * 2**30)
        health = report.data_health(s)
        s.execute(text("TRUNCATE dq_check, dataset_status"))
    assert out["status"]["backup"] == "fail" and out["status"]["pgvector"] == "ok"
    by = {d["dataset"]: d for d in health["datasets"]}
    assert by["backup"]["label"] == "Nightly backup (scripts/backup.sh)"
    assert by["pgvector"]["label"] == "pgvector extension (document search)"
    line = next(x for x in report.brief_lines(health) if x["text"].startswith("Nightly backup"))
    assert line["level"] == "warn" and "could not reach the database" in line["text"]
    assert "11.2 days old" in line["text"]


def test_the_headline_does_not_call_a_failed_backup_a_market_dataset():
    from datetime import date

    from finresearch.monitor.digest import _headline

    h = _headline([], [], [], [], date(2026, 11, 23), None, 0, 0, 0, ["Nightly backup (scripts/backup.sh)"])
    assert "1 system check failed (Nightly backup (scripts/backup.sh))" in h and "market dataset" not in h
    h = _headline([], [], [], [], date(2026, 11, 23), None, 2, 0, 0, [])
    assert "2 market datasets failed a data check" in h and "system check" not in h


def test_api_health_reports_pgvector_without_changing_ok(env, monkeypatch):
    from starlette.testclient import TestClient

    from finresearch import __version__
    from finresearch.api import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/health").json() == {"ok": True, "version": __version__, "vector": True}
        monkeypatch.setattr(misc, "VECTOR_PROBE_SQL", "select '[1]'::vector_missing_507")
        r = c.get("/api/health")
        assert r.status_code == 200 and r.json() == {"ok": True, "version": __version__, "vector": False}
        monkeypatch.setattr(misc, "VECTOR_PROBE_SQL", "select 1/0")
        assert c.get("/api/health").json()["vector"] is None  # could not be checked: unknown, not false

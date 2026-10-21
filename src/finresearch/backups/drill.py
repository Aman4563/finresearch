"""The restore drill (#375): decrypt the newest off-machine copy, restore it into a scratch *_test database, compare
row counts with the copy's manifest, record the result at the destination and drop the scratch database.

It only ever restores into a NEW database whose name ends in `_test` and is neither the configured database nor the
vault: a drill can never overwrite data. Run it quarterly (`finresearch backup drill`; docs/BACKUPS.md).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from finresearch.backups import offsite
from finresearch.backups.dump import libpq_url


class DrillRefused(RuntimeError):
    """The drill's target database is not a new scratch *_test database."""


def _names() -> set[str]:
    from finresearch.config import get_settings

    s = get_settings()
    return {make_url(u).database for u in (s.database_url, s.vault_database_url) if u}


def _admin():
    from finresearch.config import get_settings

    return create_engine(
        make_url(get_settings().database_url).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )


def check_target(name: str) -> None:
    if not name or not name.endswith("_test") or not name.replace("_", "").isalnum():
        raise DrillRefused(f"a drill restores only into a scratch database named *_test, not {name!r}")
    if name in _names():
        raise DrillRefused(f"{name!r} is the configured database: a drill never restores over it")
    eng = _admin()
    try:
        with eng.connect() as c:
            if c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).first():
                raise DrillRefused(f"{name!r} already exists: a drill restores only into a new database")
    finally:
        eng.dispose()


def default_scratch() -> str:
    from finresearch.config import get_settings

    return (make_url(get_settings().database_url).database or "finresearch").removesuffix(
        "_test"
    ) + "_drill_test"


def run(dest: Path | str | None, *, kind: str = "tables", scratch: str | None = None,
        keep_db: bool = False) -> dict[str, Any]:  # fmt: skip
    """Run one drill; returns {ok, kind, copy, expected, restored, mismatched, error} and appends it to the
    destination's drill log. {"skipped": reason} (not logged) when the scratch database cannot be created."""
    scratch = scratch or default_scratch()
    check_target(scratch)
    copy = offsite.newest(dest, kind)
    res: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "kind": kind, "copy": copy.name, "ok": False,
                           "scratch": scratch, "expected": None, "restored": None, "mismatched": [], "error": None}  # fmt: skip
    eng = _admin()
    try:
        with eng.connect() as c:
            c.execute(text(f'CREATE DATABASE "{scratch}"'))
    except Exception as e:
        eng.dispose()
        return {"skipped": f"cannot create the scratch database: {str(e)[:200]}"}
    try:
        with offsite.work_dir() as tmp:
            plain = Path(tmp) / "restore.dump"
            m = offsite.fetch(copy, plain)
            res["expected"] = m.get("counts")
            target = make_url(eng.url).set(database=scratch).render_as_string(hide_password=False)
            args = [
                "pg_restore",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                f"--dbname={libpq_url(target)}",
            ]
            if kind == "tables":  # table copies carry card_log's trigger but not its function: rows only
                args += ["--section=pre-data", "--section=data"]
            r = subprocess.run([*args, str(plain)], capture_output=True, text=True, timeout=7200)
            plain.unlink(missing_ok=True)
            if r.returncode != 0:
                res["error"] = "pg_restore failed: " + r.stderr.strip()[:300]
            else:
                res["restored"] = _counts(libpq_url(target), list(res["expected"] or ()))
                res["mismatched"] = sorted(
                    t for t, n in (res["expected"] or {}).items() if res["restored"].get(t) != n
                )
                res["ok"] = not res["mismatched"]
    except offsite.OffsiteError as e:
        res["error"] = str(e)
    finally:
        if not keep_db:
            with eng.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        eng.dispose()
    _log(dest, res)
    return res


def _counts(dsn: str, tables: list[str]) -> dict[str, int | None]:
    import psycopg

    out: dict[str, int | None] = {}
    with psycopg.connect(dsn) as conn:
        for t in tables:
            try:
                out[t] = conn.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))
                ).fetchone()[0]
            except psycopg.errors.UndefinedTable:
                conn.rollback()
                out[t] = None  # missing after a restore: unknown, never 0
    return out


def _log(dest: Path | str | None, res: dict[str, Any]) -> None:
    p = Path(dest).expanduser() / offsite.DRILL_LOG
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(res, sort_keys=True) + "\n")

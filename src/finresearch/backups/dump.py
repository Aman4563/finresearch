"""pg_dump with row counts taken in the dump's own snapshot (#375).

The counts are what a restore drill compares against, so they must describe exactly the rows in the dump: the
function opens a REPEATABLE READ transaction, exports its snapshot, counts the rows in it, and runs pg_dump with
`--snapshot` while that transaction stays open. Rows written meanwhile are in neither the counts nor the dump.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def libpq_url(url: str) -> str:
    """A SQLAlchemy URL as pg_dump/psycopg want it (no driver suffix)."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def dump_with_counts(url: str, out: Path, *, tables: Sequence[str] | None = None,
                     count: bool = True, timeout: float = 3600) -> dict[str, int] | None:  # fmt: skip
    """Dump the database at `url` (only `tables` when given) to `out` in custom format and return {table: rows}
    from the same snapshot (None when `count` is False). The archive is checked with pg_restore --list before it is
    moved into place."""
    for t in tables or ():
        if not _IDENT.match(t):
            raise ValueError(f"not a table name: {t!r}")
    dsn = libpq_url(url)
    tmp = Path(str(out) + ".partial")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snap = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        counts: dict[str, int] | None = None
        if count:
            names = list(tables) if tables else [r[0] for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")]  # fmt: skip
            counts = {t: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))).fetchone()[0]
                      for t in names}  # fmt: skip
        args = ["pg_dump", "--format=custom", "--compress=6", "--no-owner", "--no-privileges", f"--snapshot={snap}",
                f"--file={tmp}"]  # fmt: skip
        if tables:
            args += ["--strict-names", *[f"--table=public.{t}" for t in tables]]
        try:
            old = os.umask(0o077)
            try:
                subprocess.run([*args, dsn], check=True, capture_output=True, text=True, timeout=timeout)
            finally:
                os.umask(old)
            subprocess.run(["pg_restore", "--list", str(tmp)], check=True, capture_output=True, timeout=600)
            os.replace(tmp, out)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"{e.cmd[0]} failed: {(e.stderr or '').strip()[:300]}") from None
        finally:
            tmp.unlink(missing_ok=True)
            conn.execute("COMMIT")
    return counts

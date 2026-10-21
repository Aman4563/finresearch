"""What code and data a saved run was computed from (#379), so a later engine fix can never run under the same hash.

    code_hash   = SHA-256 over the sorted (path, SHA-256(bytes)) pairs of the evaluated code's *.py files
                  (ENGINE_PATHS: backtest/, direction/, fincalc/options.py, fincalc/charges.py)
    engine_hash = SHA-256("<code_hash>|<git commit>|<clean|dirty>")  (unknown commit or flag: "unknown")
    data_hash   = SHA-256 of the canonical fno_bhav_day manifest [[day, status, rows_kept], ...] over the window

`code_hash` alone answers "has the engine changed since the run?" (a new commit that touches no engine file keeps it);
`engine_hash` pins the exact checkout. A run without provenance (saved before #379) reads "unknown", never unchanged.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.config import REPO_ROOT
from finresearch.db.models import FnoBhavDay

REPO = REPO_ROOT
ENGINE_PATHS = ("src/finresearch/backtest", "src/finresearch/direction", "src/finresearch/fincalc/options.py",
                "src/finresearch/fincalc/charges.py")  # fmt: skip


def _files(root: Path, paths: Sequence[str]) -> list[tuple[str, Path]]:
    out = []
    for rel in paths:
        p = root / rel
        if not p.exists():
            raise FileNotFoundError(f"engine path {rel} is missing under {root}")  # never hash a smaller tree
        found = [p] if p.is_file() else sorted(x for x in p.rglob("*.py") if "__pycache__" not in x.parts)
        out += [(x.relative_to(root).as_posix(), x) for x in found]
    return sorted(out)


def code_hash(root: Path, paths: Sequence[str] = ENGINE_PATHS) -> str:
    h = hashlib.sha256()
    for rel, p in _files(root, paths):
        h.update(rel.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def git_state(root: Path, paths: Sequence[str] = ENGINE_PATHS) -> tuple[str | None, bool | None]:
    """(HEAD commit, whether the engine paths have uncommitted changes); (None, None) when git can't say."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10
        )
        st = subprocess.run(["git", "status", "--porcelain", "--", *paths], cwd=root, capture_output=True, text=True,
                            timeout=10)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None, None
    if head.returncode or st.returncode:
        return None, None
    return head.stdout.strip() or None, bool(st.stdout.strip())


def engine_hash(code: str, commit: str | None, dirty: bool | None) -> str:
    flag = "unknown" if dirty is None else ("dirty" if dirty else "clean")
    return hashlib.sha256(f"{code}|{commit or 'unknown'}|{flag}".encode()).hexdigest()


def data_hash(s: Session, start: date, end: date | None) -> dict[str, Any]:
    """The fno_bhav_day manifest's hash over [start, end] (end None = every stored day from start)."""
    q = select(FnoBhavDay.day, FnoBhavDay.status, FnoBhavDay.rows_kept).where(FnoBhavDay.day >= start)
    if end is not None:
        q = q.where(FnoBhavDay.day <= end)
    rows = [[d.isoformat(), st, kept] for d, st, kept in s.execute(q.order_by(FnoBhavDay.day))]
    return {"data_hash": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest(),
            "data_days": len(rows), "data_from": rows[0][0] if rows else None,
            "data_to": rows[-1][0] if rows else None}  # fmt: skip


def code_stamp(root: Path = REPO) -> dict[str, Any]:
    code = code_hash(root)
    commit, dirty = git_state(root)
    return {"engine_hash": engine_hash(code, commit, dirty), "code_hash": code, "git_commit": commit,
            "git_dirty": dirty, "engine_paths": list(ENGINE_PATHS)}  # fmt: skip


@lru_cache(maxsize=1)
def current_code_hash() -> str:
    """This process's engine code hash, read once: the code a long-running server executes is what it imported."""
    return code_hash(REPO)


def code_changed(run: dict[str, Any]) -> str | None:
    """Why the run's engine code differs from the running code (None = the same code)."""
    was = run.get("code_hash")
    if not was:
        return "engine provenance unknown: the run was saved without a code hash (before #379)"
    try:
        now = current_code_hash()
    except OSError:
        return "engine code of this install could not be read, so the run's code cannot be shown to match"
    return None if was == now else "engine code changed since the run (code_hash differs): re-run it"

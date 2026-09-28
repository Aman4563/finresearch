"""Run workers: every research run started from the app is a separate `finresearch ipo resume <id> --wait` process.

The API never calls Claude itself. A worker process runs exactly the CLI code path, keeps running if the API
restarts, and resumes from its finished steps if it is started again. The pid, command and log path are stored in
the run manifest so the app can show whether a run is being worked on.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finresearch.config import get_settings
from finresearch.db import session_scope
from finresearch.db.models import ResearchRun


def worker_command(run_id: int, *, streams: list[str] | None = None, concurrency: int = 4) -> list[str]:
    argv = [sys.executable, "-m", "finresearch.cli", "ipo", "resume", str(run_id), "--wait",
            "--concurrency", str(concurrency)]  # fmt: skip
    if streams:
        argv += ["--streams", ",".join(streams)]
    return argv


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def worker_info(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    w = (manifest or {}).get("worker")
    return {**w, "alive": pid_alive(w.get("pid"))} if w else None


def _popen(argv: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, env=os.environ.copy())  # fmt: skip
    return proc.pid


class WorkerBusy(RuntimeError):
    pass


@dataclass
class Spawner:
    """Starts run workers; tests replace `popen` so no real pipeline process is launched."""

    popen: Callable[[list[str], Path], int] = _popen

    def start(self, run_id: int, *, streams: list[str] | None = None, concurrency: int = 4) -> dict[str, Any]:
        with session_scope() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                raise LookupError(f"unknown run {run_id}")
            current = worker_info(run.manifest)
            if current and current["alive"]:
                raise WorkerBusy(f"run {run_id} already has a live worker (pid {current['pid']})")
            argv = worker_command(run_id, streams=streams, concurrency=concurrency)
            log = get_settings().runs_dir / str(run_id) / "worker.log"
            pid = self.popen(argv, log)
            info = {
                "pid": pid,
                "argv": argv[1:],
                "log": str(log),
                "started_at": datetime.now(UTC).isoformat(),
            }
            run.manifest = {**(run.manifest or {}), "worker": info}
            return info

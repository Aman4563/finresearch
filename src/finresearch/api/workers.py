"""Run workers: every research run started from the app is a separate `finresearch ipo resume <id> --wait` process.

The API never calls Claude itself. A worker process runs exactly the CLI code path, keeps running if the API
restarts, and resumes from its finished steps if it is started again. The pid, command and log path are stored in
the run manifest so the app can show whether a run is being worked on.

Keeping the Mac awake: a worker runs for an hour or more, and a Mac set to sleep after a minute of inactivity would
put it to sleep mid-response ("API Error: Your computer went to sleep mid-response"). Every worker is therefore
watched by `caffeinate -is -w <pid>` (macOS only, when `caffeinate` is on PATH): an idle-sleep assertion (and a
system-sleep one on mains power) that lasts exactly as long as the worker process and ends by itself when the worker
exits. It changes no system setting; closing the lid still sleeps the Mac. `finresearch ipo run/resume` started from
a terminal does the same for itself. Turn it off with FINRESEARCH_KEEP_AWAKE=false.

A run whose status is "running" but whose worker has exited is reported as stalled (`stalled_info`), and the app
offers Resume. `auto_resume_due` (called by the API's monitor loop) restarts runs paused by transient errors
whose worker has exited once their resume time has passed; `Spawner.start` holds the run's row lock, so it never
starts a second worker.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from finresearch.config import get_settings
from finresearch.db import session_scope
from finresearch.db.models import AgentStep, ResearchRun

log = logging.getLogger(__name__)
AWAKE_ENV = "FINRESEARCH_CAFFEINATED"  # set for a worker whose parent already keeps the Mac awake for it
MAX_AUTO_RESUMES = 5  # automatic restarts of one run for transient pauses, before a person has to look at it
STALL_WITHOUT_WORKER = timedelta(
    hours=2
)  # a run with no recorded worker counts as stalled after this long idle


def worker_command(
    run_id: int, *, streams: list[str] | None = None, concurrency: int | None = None
) -> list[str]:
    """Without streams/concurrency the CLI resumes with the ones saved when the run started."""
    argv = [sys.executable, "-m", "finresearch.cli", "ipo", "resume", str(run_id), "--wait"]
    if concurrency is not None:
        argv += ["--concurrency", str(concurrency)]
    if streams:
        argv += ["--streams", ",".join(streams)]
    return argv


_children: dict[int, subprocess.Popen] = {}  # workers this process started: poll() reaps them when they exit


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if (proc := _children.get(pid)) is not None:
        if proc.poll() is None:
            return True
        del _children[pid]
        return False
    try:  # a child we lost track of would stay a zombie (and pass kill(pid, 0)) until reaped
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass  # not our child (started by an earlier API process or the CLI)
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


def caffeinate_argv(pid: int, *, platform: str | None = None, which=shutil.which) -> list[str] | None:
    """The macOS `caffeinate` command that keeps the Mac awake while `pid` runs, or None where it is unavailable."""
    if (platform or sys.platform) != "darwin" or not get_settings().keep_awake:
        return None
    exe = which("caffeinate")
    return [exe, "-i", "-s", "-w", str(pid)] if exe else None


_keepers: list[subprocess.Popen] = []


def keep_awake(pid: int, *, platform: str | None = None, which=shutil.which, popen=subprocess.Popen) -> bool:
    """Start `caffeinate -is -w <pid>` (it exits by itself when pid exits). Never raises: sleep prevention is a
    convenience, not a reason to fail a run."""
    argv = caffeinate_argv(pid, platform=platform, which=which)
    if argv is None:
        return False
    _keepers[:] = [k for k in _keepers if k.poll() is None]  # reap the ones whose worker has exited
    try:
        _keepers.append(popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True))  # fmt: skip
    except OSError as e:
        log.warning("could not start caffeinate for pid %s: %s", pid, e)
        return False
    return True


def _popen(argv: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    awake = caffeinate_argv(0) is not None
    if awake:
        env[AWAKE_ENV] = "1"  # the worker's CLI does not start a second caffeinate
    with log_path.open("ab") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, env=env)  # fmt: skip
    _children[proc.pid] = proc
    if awake:
        keep_awake(proc.pid)
    return proc.pid


class WorkerBusy(RuntimeError):
    pass


@dataclass
class Spawner:
    """Starts run workers; tests replace `popen` so no real pipeline process is launched."""

    popen: Callable[[list[str], Path], int] = _popen

    def start(
        self,
        run_id: int,
        *,
        streams: list[str] | None = None,
        concurrency: int | None = None,
        note: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """`note`: extra manifest fields written under the same row lock as the worker record."""
        with session_scope() as s:
            run = s.get(ResearchRun, run_id, with_for_update=True)  # one worker even for two quick clicks
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
            run.manifest = {**(run.manifest or {}), **(note or {}), "worker": info}
            return info


def claim_worker(run_id: int, argv: list[str]) -> None:
    """Record this process as the run's worker (the CLI path), refusing if another live worker has it."""
    pid = os.getpid()
    with session_scope() as s:
        run = s.get(ResearchRun, run_id, with_for_update=True)
        if run is None:
            return
        current = worker_info(run.manifest)
        if current and current["pid"] != pid and current["alive"]:
            raise WorkerBusy(f"run {run_id} already has a live worker (pid {current['pid']})")
        if current and current["pid"] == pid:
            return  # started by Spawner: already recorded
        info = {
            "pid": pid,
            "argv": argv,
            "log": None,
            "started_at": datetime.now(UTC).isoformat(),
            "source": "cli",
        }
        run.manifest = {**(run.manifest or {}), "worker": info}


def stalled_info(run: ResearchRun, worker: dict[str, Any] | None, last_activity: datetime | None,
                 *, now: datetime | None = None) -> dict[str, Any] | None:  # fmt: skip
    """A run marked running that nothing is working on: its worker exited (crash, kill, reboot) or, for a run with
    no recorded worker, nothing has happened for STALL_WITHOUT_WORKER. None when the run is not stalled."""
    if run.status != "running":
        return None
    now = now or datetime.now(UTC)
    since = last_activity or run.created_at
    if worker:
        if worker.get("alive"):
            return None
        reason = f"The worker (pid {worker.get('pid')}) is no longer running"
    else:
        if since is not None and now - since < STALL_WITHOUT_WORKER:
            return None
        reason = "No worker is recorded for this run and nothing has changed"
    return {
        "reason": f"{reason}, but the run is still marked running.",
        "since": since.isoformat() if since else None,
    }


def last_activity(s, run_ids: list[int]) -> dict[int, datetime]:
    from sqlalchemy import func, select

    if not run_ids:
        return {}
    q = (select(AgentStep.run_id, func.max(func.greatest(AgentStep.started_at, AgentStep.finished_at)))
         .where(AgentStep.run_id.in_(run_ids)).group_by(AgentStep.run_id))  # fmt: skip
    return {rid: ts for rid, ts in s.execute(q) if ts is not None}


def auto_resume_due(spawner: Spawner, *, now: datetime | None = None) -> list[int]:
    """Start a worker for each run paused by transient errors whose resume time has passed and whose worker has
    exited (a worker started with --wait resumes by itself). Bounded by MAX_AUTO_RESUMES per run."""
    from sqlalchemy import select

    if not get_settings().auto_resume_transient:
        return []
    now = now or datetime.now(UTC)
    started: list[int] = []
    with session_scope() as s:
        due = s.scalars(
            select(ResearchRun).where(ResearchRun.status == "paused", ResearchRun.resume_after <= now)
        ).all()
        candidates = [(r.id, r.manifest or {}) for r in due]
    for run_id, m in candidates:
        if m.get("pause_kind") != "transient" or int(m.get("auto_resumes") or 0) >= MAX_AUTO_RESUMES:
            continue
        if (worker_info(m) or {}).get("alive"):
            continue
        try:  # start() takes the row lock and re-checks the worker: never two workers
            spawner.start(run_id, note={"auto_resumes": int(m.get("auto_resumes") or 0) + 1})
        except (WorkerBusy, LookupError):
            continue
        log.info("auto-resumed run %s after a transient pause", run_id)
        started.append(run_id)
    return started

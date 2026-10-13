"""Strategy Lab runs as background jobs (#318): the API queues a `lab_run` row and starts a worker process
(`finresearch fno lab run-job <id>`), never replaying in the request. The worker reads the stored F&O history through
`backtest.dbhistory.DbHistory` (every transaction SET READ ONLY), writes its progress to the row at most every
HEARTBEAT_S seconds and stops itself once RUN_CAP has passed (status timed_out). A run whose worker stopped
reporting is marked failed by `recover_stale` (called whenever runs are listed or queued).

At the end, under one advisory lock (so two finishing runs cannot both read the same N): an out-of-sample run of a
spec never evaluated out of sample before adds it to the variants counter (`lab_variant`); then N, V, the deflated
Sharpe, the criteria and the warnings are computed (stratlab.evaluate.finalize) and stored with every trade.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from finresearch.db import session_scope
from finresearch.db.models import LabRun, LabStrategy, LabVariant, LabVersion
from finresearch.stratlab import evaluate as ev
from finresearch.stratlab import store
from finresearch.stratlab.spec import StrategySpec

log = logging.getLogger(__name__)

RUN_CAP = timedelta(minutes=15)  # a run that is not done by then stops (timed_out)
HEARTBEAT_S = 2.0
STALE_AFTER = timedelta(minutes=3)  # no progress written for this long: the worker is gone
START_GRACE = timedelta(minutes=2)  # a queued run no worker picked up
MAX_ACTIVE = 2  # runs queued or running at once (each one reads the history store hard)
ACTIVE = ("queued", "running")
LOCK_KEY = 318_001  # pg_advisory_xact_lock: finishing runs, one at a time


class RunTimeout(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- queue and launch
_children: dict[int, subprocess.Popen] = {}


def launch(run_id: int) -> int | None:
    """Start the worker process for a queued run; its output goes to <state_dir>/lab/run_<id>.log."""
    from finresearch.config import get_settings

    for pid, p in list(_children.items()):  # reap finished workers
        if p.poll() is not None:
            _children.pop(pid, None)
    path = get_settings().state_dir / "lab" / f"run_{run_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "finresearch.cli", "fno", "lab", "run-job", str(run_id)]
    with path.open("ab") as out:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True, env=os.environ.copy())  # fmt: skip
    _children[proc.pid] = proc
    return proc.pid


LAUNCHER: Callable[[int], int | None] = launch  # tests swap this for an in-process runner


def submit(s: Session, version_id: int, oos: bool, now: datetime | None = None) -> LabRun:
    """Queue a run (the caller commits, then calls LAUNCHER). Raises store.LabError when refused."""
    now = now or _now()
    recover_stale(s, now)
    if s.get(LabVersion, version_id) is None:
        raise store.LabError(f"no lab version {version_id}")
    same = s.scalars(select(LabRun).where(LabRun.version_id == version_id, LabRun.oos.is_(oos),
                                          LabRun.status.in_(ACTIVE))).first()  # fmt: skip
    if same is not None:
        raise store.LabError(f"run {same.id} of this version is already {same.status}")
    busy = s.scalar(select(func.count()).select_from(LabRun).where(LabRun.status.in_(ACTIVE))) or 0
    if busy >= MAX_ACTIVE:
        raise store.LabError(f"{busy} lab runs are already queued or running; try again when one finishes")
    r = LabRun(
        version_id=version_id, oos=oos, status="queued", progress=0.0, counted=False, result={}, trades=[]
    )
    s.add(r)
    s.flush()
    return r


def recover_stale(s: Session, now: datetime | None = None) -> int:
    """Mark runs whose worker is gone: queued and never started, or running without progress."""
    now = now or _now()
    n = 0
    for r in s.scalars(select(LabRun).where(LabRun.status.in_(ACTIVE)).with_for_update(skip_locked=True)):
        if r.status == "queued" and r.created_at is not None and now - r.created_at > START_GRACE:
            r.status, r.error = "failed", "the worker did not start"
        elif r.status == "running" and (r.heartbeat_at is None or now - r.heartbeat_at > STALE_AFTER):
            r.status, r.error = (
                "failed",
                f"the worker stopped reporting progress (no update for {STALE_AFTER})",
            )
        else:
            continue
        r.finished_at = now
        n += 1
    return n


# --------------------------------------------------------------------------- the worker
def _set(run_id: int, **kw: Any) -> None:
    with session_scope() as s:
        r = s.get(LabRun, run_id)
        if r is not None:
            for k, v in kw.items():
                setattr(r, k, v)


def execute(run_id: int, history: Any = None, *, cap: timedelta = RUN_CAP,
            clock: Callable[[], datetime] = _now) -> str:  # fmt: skip
    """Run one queued lab run to the end; returns its final status. `history` defaults to the app's stored history."""
    with session_scope() as s:
        r = s.get(LabRun, run_id, with_for_update=True)
        if r is None or r.status != "queued":
            return r.status if r is not None else "missing"
        v = s.get(LabVersion, r.version_id)
        st = s.get(LabStrategy, v.strategy_id)
        spec, split, oos = StrategySpec.model_validate(v.spec), store.split_of(st), r.oos
        key, name, sid, vid = f"lab{st.id}v{v.version}", f"{st.name} (v{v.version})", st.id, v.id
        t0 = clock()
        r.status, r.started_at, r.heartbeat_at, r.pid = "running", t0, t0, os.getpid()
    if history is None:
        from finresearch.backtest.dbhistory import DbHistory

        history = DbHistory()
    deadline, last = t0 + cap, [0.0]

    def progress(frac: float, note: str) -> None:
        t = clock()
        if t > deadline:
            raise RunTimeout(f"stopped after the {int(cap.total_seconds() // 60)}-minute cap ({note})")
        if time.monotonic() - last[0] >= HEARTBEAT_S:
            last[0] = time.monotonic()
            _set(run_id, progress=round(min(frac, 0.99), 4), progress_note=note[:200], heartbeat_at=t)

    try:
        rep = ev.replay(history, spec, split, key=key, name=name, oos=oos, progress=progress)
    except RunTimeout as e:
        _set(run_id, status="timed_out", error=str(e), finished_at=clock())
        return "timed_out"
    except Exception as e:
        log.warning("lab run %s failed", run_id, exc_info=True)
        _set(run_id, status="failed", error=f"{type(e).__name__}: {e}"[:500], finished_at=clock())
        return "failed"
    return _finish(run_id, rep, sid, vid, clock())


def _finish(run_id: int, rep: dict[str, Any], sid: int, vid: int, now: datetime) -> str:
    rep = json.loads(json.dumps(rep, default=str))
    with session_scope() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        r = s.get(LabRun, run_id, with_for_update=True)
        if r is None or r.status != "running":  # marked stale meanwhile: keep that verdict
            return r.status if r is not None else "missing"
        counted = False
        if rep.get("oos") and rep.get("status") != "no_data":
            h = rep["spec_hash"]
            if s.scalars(select(LabVariant).where(LabVariant.spec_hash == h)).first() is None:
                s.add(LabVariant(spec_hash=h, version_id=vid, run_id=run_id,
                                 daily_sr=rep["windows"]["oos"].get("daily_sr")))  # fmt: skip
                s.flush()
                counted = True
        n = store.n_trials(s)
        var = ev.trials_variance(store.trial_srs(s))
        rep = ev.finalize(rep, n_trials=n, var=var, oos_versions=store.oos_versions(s, sid),
                          n_lab=store.lab_variants(s))  # fmt: skip
        trades = rep.pop("trades", [])
        r.result, r.trades, r.n_trials, r.counted = rep, trades, n, counted
        r.status, r.progress, r.progress_note, r.finished_at, r.heartbeat_at = "done", 1.0, None, now, now
        return "done"

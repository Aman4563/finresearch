"""The monitor's monthly re-validation (#379, inventory.revalidate): the registered backtest recipes replayed read-only,
their per-regime-split statistics appended to model_revalidation. Never the saved backtest the cards read.

When. Once per calendar month (slot "revalidate:YYYY-MM" in alert_eval_slot), from RUN_AFTER (22:30 IST) on any day
of the month, so a missed night runs the next one. 22:30 is after the evening bhavcopy (20:30) and the skill pass
(21:30) and outside market hours whatever the holiday list says; due_slot also refuses a time inside the session
(batchwindow.in_market_hours: an unknown holiday list counts as trading, which protects the live app).

How. The replay takes minutes of CPU, so it never runs in the API process: `revalidate_step` (in a worker thread)
starts `python -m finresearch.cli models revalidate --slot <slot>` through batchwindow.run_batch, i.e. under macOS
background QoS (`taskpolicy -b`, else `nice`), deferred or paused inside market hours. A non-zero exit or a deferral
is a failed attempt, retried (monitor.disclosures MAX_ATTEMPTS, RETRY_AFTER apart); rows a failed attempt already
appended are kept, and the retry appends only the missing ones (unique per slot, model and split).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime, time
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(22, 30)
PREFIX = "revalidate:"


def due_slot(now: datetime) -> str | None:
    from finresearch.batchwindow import in_market_hours

    ist = to_ist(now)
    if ist.time() < RUN_AFTER or in_market_hours(now):
        return None
    return f"{PREFIX}{ist:%Y-%m}"


def worker_argv(slot: str) -> list[str]:
    return [sys.executable, "-m", "finresearch.cli", "models", "revalidate", "--slot", slot]


def _run_worker(slot: str) -> int | None:
    """The worker's exit code, or None when the batch window deferred it."""
    from finresearch.batchwindow import run_batch

    res = run_batch(worker_argv(slot))
    return None if res.deferred else res.returncode


def revalidate_step(now: datetime, runner: Callable[[str], int | None] = _run_worker) -> dict[str, Any]:
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now)
    if slot is None or not claim(slot, now):
        return {}
    try:
        rc = runner(slot)
    except Exception as e:
        log.warning("re-validation worker could not start; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"revalidate": "failed"}
    if rc != 0:
        why = "deferred: inside market hours" if rc is None else f"worker exited with {rc}"
        finish(slot, {"returncode": rc, "error": why}, now, failed=True)
        return {"revalidate": "failed"}
    finish(slot, {"returncode": 0}, now, failed=False)
    return {"revalidate": "done"}

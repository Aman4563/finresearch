"""The monitor's data-quality pass (#368, finresearch.dq.run): database only, no network.

Two windows on trading days (monitor.market_days fails closed: an unknown holiday list skips the pass, and the last
stored statuses stay as they were):
- evening, from EVENING (21:00 IST): after every evening SLA (bhavcopy 20:30), so tonight's files are checked;
- morning, MORNING to BRIEF (08:00-08:30 IST): again before the 08:30 brief, so a file that came in late at night
  is no longer reported stale.
The issue proposed about 19:45; that is before the bhavcopy's own 20:30 SLA, so the evening pass runs at 21:00 (listed
in the PR as a decision). Each pass is claimed in `alert_eval_slot` ("dq:evening:<day>"); a crash is retried
(monitor.disclosures MAX_ATTEMPTS, RETRY_AFTER apart). Failing checks are results, not a failed pass.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

EVENING = time(21, 0)
MORNING = time(8, 0)
BRIEF = time(8, 30)


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if ist.time() >= EVENING:
        kind = "evening"
    elif MORNING <= ist.time() < BRIEF:
        kind = "morning"
    else:
        return None
    if not market_day(ist.date(), holidays, job="data-quality checks"):
        return None
    return f"dq:{kind}:{ist.date().isoformat()}"


def dq_step(now: datetime, holidays: set[date] | None = None,
            free_bytes: Callable[[Any], int] | None = None) -> dict[str, Any]:  # fmt: skip
    from finresearch.db import session_scope
    from finresearch.dq import run
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now, holidays)
    if slot is None or not claim(slot, now):
        return {}
    try:
        with session_scope() as s:
            res = run.run_all(s, now, holidays=holidays, free_bytes=free_bytes)
    except Exception as e:
        log.warning("data-quality pass failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"dq": "failed"}
    finish(
        slot, {"checks": res["checks"], "datasets": res["status"]}, now, failed=False
    )  # "status" is the slot's
    return {"dq": res}

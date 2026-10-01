"""The monitor's daily fund category ranking (signals.fund_rank): once a day from RUN_AFTER (IST).

The ranking is as of the last month-end, so most days it only re-reads the cached month-end snapshots, NAVAll and
the TER file; the first run after a month-end adds one AMFI request for that month-end (and the very first run
fills in up to 61 month-ends, at the polite client's pace of one request a second). Each day's run is claimed in
`alert_eval_slot` ("fund_ranks:<day>"), so two monitor processes never run it twice; a failed run is retried up to
monitor.disclosures.MAX_ATTEMPTS times, RETRY_AFTER apart. Pages and alerts only read the stored result.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(7, 0)  # AMFI has every NAV of the previous day by then (published by about 23:00 IST)


def due_slot(now: datetime) -> str | None:
    ist = to_ist(now)
    return f"fund_ranks:{ist.date().isoformat()}" if ist.time() >= RUN_AFTER else None


async def fund_ranks_step(now: datetime) -> dict[str, Any]:
    from finresearch.monitor.disclosures import claim, finish
    from finresearch.signals import fund_rank

    slot = due_slot(now)
    if slot is None or not claim(slot, now):
        return {}
    try:
        res = await fund_rank.refresh(to_ist(now).date())
    except Exception as e:
        log.warning("fund category ranking failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"fund_ranks": "failed"}
    finish(slot, res, now, failed=False)
    return {"fund_ranks": res}

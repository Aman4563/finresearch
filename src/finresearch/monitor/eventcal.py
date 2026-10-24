"""The monitor's nightly event-calendar refresh (#367, eventcal.refresh): once an evening from RUN_AFTER (IST), well
outside NSE's 09:00-16:00 session, the sources the owner accepted on 23-Oct-2026 (#481) are read: RBI's press-release
RSS feed for MPC schedules, rescheduling notices and resolutions (eventcal.rbi, 1 request, never cached), MoSPI's
CPI rule and NSE Indices' NIFTY review rule (eventcal.rules, no request), MoSPI's CPI API to confirm a CPI release
that has come (eventcal.rules.confirm_cpi, only on such nights) and NSE's circulars listing for expiry-day revisions
(notices) and index-change circulars (eventcal.nse, 1 request and a warm-up). The publishers do not follow NSE's
trading calendar, so the job runs every day and does not consult the NSE holiday list. The night is claimed in
`alert_eval_slot` ("eventcal:<day>"); a night where a decided source failed is retried (monitor.disclosures
MAX_ATTEMPTS), and a failed source stores nothing, so coverage stays where it was and plans past it read "unknown". A
source without the owner's data-rights decision is recorded as skipped: no request, no retry.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(21, 0)  # after RBI's and MoSPI's evening releases; never in session


def due_slot(now: datetime) -> str | None:
    ist = to_ist(now)
    if ist.time() < RUN_AFTER:
        return None
    return f"eventcal:{ist.date().isoformat()}"


async def eventcal_step(now: datetime, *, sources: list[Any] | None = None) -> dict[str, Any]:
    """Claim tonight's slot and refresh every decided source. `sources` replaces the live list (tests)."""
    from finresearch.eventcal import refresh
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now)
    if slot is None or not claim(slot, now):
        return {}
    try:
        res = await refresh.run(refresh.sources() if sources is None else sources, now)
    except Exception as e:
        log.warning("event calendar refresh failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"eventcal": "failed"}
    finish(slot, res, now, failed=bool(res["failed"]))
    return {"eventcal": res}

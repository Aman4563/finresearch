"""The monitor's event-calendar refresh (#367, eventcal.refresh). RBI's RSS feed alone at RSS_TIMES, then once an evening from RUN_AFTER (IST), well
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
# RBI's feed holds only its 10 newest items, so on a busy day one nightly read can miss an MPC release (#481). The
# owner chose four reads a day (26-Oct-2026): these daytime windows read the RSS feed alone (one small request to RBI,
# never NSE, so the 09:00-16:00 NSE rule does not apply); the 21:00 run reads every source.
RSS_TIMES = (time(6, 0), time(12, 0), time(16, 30))
RSS_SOURCE = "rbi_rss"


def due_slot(now: datetime) -> str | None:
    """The full nightly slot from RUN_AFTER, else the latest daytime RSS window that has opened today."""
    ist = to_ist(now)
    if ist.time() >= RUN_AFTER:
        return f"eventcal:{ist.date().isoformat()}"
    opened = [t for t in RSS_TIMES if ist.time() >= t]
    return f"eventcal-rss:{ist.date().isoformat()}T{opened[-1]:%H%M}" if opened else None


async def eventcal_step(now: datetime, *, sources: list[Any] | None = None) -> dict[str, Any]:
    """Claim the slot that is due and refresh: every decided source at night, RBI's feed alone in a daytime window.
    `sources` replaces the live list (tests)."""
    from finresearch.eventcal import refresh
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now)
    if slot is None or not claim(slot, now):
        return {}
    todo = refresh.sources() if sources is None else sources
    if slot.startswith("eventcal-rss:"):
        todo = [s for s in todo if s.dataset == RSS_SOURCE]
    try:
        res = await refresh.run(todo, now)
    except Exception as e:
        log.warning("event calendar refresh failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"eventcal": "failed"}
    finish(slot, res, now, failed=bool(res["failed"]))
    return {"eventcal": res}

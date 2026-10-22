"""The monitor's nightly FBIL rates update (#366, refseries.rates): once per weekday evening from RUN_AFTER (IST),
the last LOOKBACK of every series: the JSON fixings (two monthly windows per series, ~1-5 KB each) and the G-sec
workbooks of any newly published day (~0.73 MB each, normally one). FBIL's public pages lag about a week, so the
window is re-read each night and upserted. FBIL follows the RBI/money-market calendar, not NSE's, so the job runs on
every weekday and does not consult the NSE holiday list (a holiday only means an empty answer). The night is claimed
in `alert_eval_slot` ("rates:<day>"); a failed pass is retried (monitor.disclosures MAX_ATTEMPTS). Without the
owner's data-rights decision for FBIL (data_rights.PENDING) the night is recorded as skipped: no request, no retry.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(20, 30)  # after the T-bill (17:30) and G-sec publications
LOOKBACK = timedelta(days=45)


def due_slot(now: datetime) -> str | None:
    ist = to_ist(now)
    if ist.time() < RUN_AFTER or ist.weekday() >= 5:
        return None
    return f"rates:{ist.date().isoformat()}"


async def rates_step(now: datetime, **kw: Any) -> dict[str, Any]:
    """Claim tonight's slot and re-read the recent window. `kw` goes to refseries.rates.backfill (tests)."""
    from finresearch import data_rights
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now)
    if slot is None or not claim(slot, now):
        return {}
    today = to_ist(now).date()
    try:
        from finresearch.refseries import rates as store

        res = await store.backfill(store.SERIES, today - LOOKBACK, today, today=today, now=lambda: now, **kw)
    except data_rights.NoDecision as e:
        finish(slot, {"skipped": str(e)[:300]}, now, failed=False)
        return {"rates": {"skipped": str(e)}}
    except Exception as e:
        log.warning("FBIL rates update failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"rates": "failed"}
    finish(slot, res, now, failed=bool(res["failed"]) or bool(res.get("stopped")))
    return {"rates": res}

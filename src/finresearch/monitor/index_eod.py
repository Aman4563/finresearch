"""The monitor's nightly index close update (#366, refseries.index_eod): once per trading-day evening from RUN_AFTER
(IST), NSE's ind_close_all file for today (one GET, ~9 KB), plus any of the last LOOKBACK_DAYS days missed while the
Mac was off. When today's file is not there yet the run counts as failed and is retried (monitor.disclosures
MAX_ATTEMPTS, 30 minutes apart). Weekends and NSE holidays are skipped; an unknown holiday list skips the run too
(monitor.market_days fails closed). The night is claimed in `alert_eval_slot` ("index_eod:<day>"), so two monitor
processes never run it twice. Older history is the CLI's job (`finresearch index backfill`), outside market hours.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(19, 30)  # with the F&O bhavcopy job; NSE posts the day's index file in the evening
LOOKBACK_DAYS = 4


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if ist.time() < RUN_AFTER or not market_day(ist.date(), holidays, job="index close file"):
        return None
    return f"index_eod:{ist.date().isoformat()}"


async def index_eod_step(now: datetime, holidays: set[date] | None = None, **kw: Any) -> dict[str, Any]:
    """Claim tonight's slot and fetch the missing recent days. `kw` goes to refseries.index_eod.backfill (tests)."""
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now, holidays)
    if slot is None or not claim(slot, now):
        return {}
    today = to_ist(now).date()
    try:
        from finresearch.refseries import index_eod as store

        res = await store.backfill(today - timedelta(days=LOOKBACK_DAYS), today, today=today, holidays=holidays,
                                   now=lambda: now, **kw)  # fmt: skip
    except Exception as e:
        log.warning("index close update failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"index_eod": "failed"}
    pending = not _loaded(today)
    summary = res | {"today_pending": pending}
    finish(slot, summary, now, failed=pending or bool(res["failed"]))
    return {"index_eod": summary}


def _loaded(day: date) -> bool:
    from finresearch.db import session_scope
    from finresearch.db.models import IndexEodDay

    with session_scope() as s:
        m = s.get(IndexEodDay, day)
        return m is not None and m.status == "loaded"

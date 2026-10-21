"""The monitor's nightly SPAN update (#370, fnohist.span): once per trading-day evening from RUN_AFTER (IST), NSE
Clearing's SPAN file for today (one GET, ~10 MB zipped; the 16-Oct-2026 file was created at 16:54 IST) and its FOVOLT
file (one GET, ~40 KB), plus any of the last LOOKBACK_DAYS days missed while the Mac was off. The zip is kept for
the last fnohist.span.KEEP_SESSIONS sessions (the full risk arrays) and older ones are deleted. When today's file is
not there yet the run counts as failed and is retried (monitor.disclosures MAX_ATTEMPTS, 30 minutes apart). Weekends
and NSE holidays are skipped; an unknown holiday list skips the run too (monitor.market_days fails closed). The night
is claimed in `alert_eval_slot` ("span:<day>"), so two monitor processes never run it twice.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(19, 30)
LOOKBACK_DAYS = 4


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if ist.time() < RUN_AFTER or not market_day(ist.date(), holidays, job="SPAN file"):
        return None
    return f"span:{ist.date().isoformat()}"


async def span_step(now: datetime, holidays: set[date] | None = None, **kw: Any) -> dict[str, Any]:
    """Claim tonight's slot and fetch the missing recent days. `kw` goes to fnohist.span.backfill (tests)."""
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now, holidays)
    if slot is None or not claim(slot, now):
        return {}
    today = to_ist(now).date()
    try:
        from finresearch.fnohist import span

        kw.setdefault("keep_raw", True)
        res = await span.backfill(today - timedelta(days=LOOKBACK_DAYS), today, today=today, monthly=False,
                                  holidays=holidays, now=lambda: now, **kw)  # fmt: skip
    except Exception as e:
        log.warning("SPAN update failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"span": "failed"}
    pending = not _loaded(today)
    summary = res | {"today_pending": pending}
    finish(slot, summary, now, failed=pending or bool(res["failed"]))
    return {"span": summary}


def _loaded(day: date) -> bool:
    from finresearch.db import session_scope
    from finresearch.db.models import SpanDay

    with session_scope() as s:
        m = s.get(SpanDay, day)
        return m is not None and m.status == "loaded"

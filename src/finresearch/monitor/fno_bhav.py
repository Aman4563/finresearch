"""The monitor's nightly F&O bhavcopy update (#306, fnohist.store): once per trading-day evening from RUN_AFTER (IST).

NSE publishes the day's F&O bhavcopy in the evening (zip entry 17:56 IST on 30-Sep-2026, Last-Modified 18:59 and
19:17 IST on other days: adapters.nse_fno_bhav), so the job starts at 19:30. It runs the same resumable backfill over
the last LOOKBACK_DAYS days: normally one download (today's file), plus any weekend special session, any day missed
while the Mac was off, and any recent day whose file was late. When today's file is not there yet the run counts as
failed and is retried (monitor.disclosures.MAX_ATTEMPTS, 30 minutes apart); the next evening's run picks it up
otherwise. After loading, the day's derived IV row per underlying (fno_iv_eod) and the contract calendar are updated.
Weekends and NSE holidays are skipped; an unknown holiday list skips the run too (monitor.market_days fails closed).
Each night is claimed in `alert_eval_slot` ("fno_bhav:<day>"), so two monitor processes never run it twice.

The same evening NSE's participant-wise F&O open interest file (#317, fnohist.participants) is fetched in its own slot
("fno_participant:<day>"), so a failure of one never retries or fails the other.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(19, 30)
LOOKBACK_DAYS = 10


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if ist.time() < RUN_AFTER or not market_day(ist.date(), holidays, job="F&O bhavcopy"):
        return None
    return f"fno_bhav:{ist.date().isoformat()}"


async def fno_bhav_step(now: datetime, holidays: set[date] | None = None, **kw: Any) -> dict[str, Any]:
    """Claim tonight's slot and update the last LOOKBACK_DAYS days. `kw` goes to fnohist.store.backfill (tests)."""
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now, holidays)
    if slot is None or not claim(slot, now):
        return {}
    today = to_ist(now).date()
    try:
        from finresearch.fnohist import store

        res = await store.backfill(today - timedelta(days=LOOKBACK_DAYS), today, today=today, **kw)
    except Exception as e:
        log.warning("F&O bhavcopy update failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"fno_bhav": "failed"}
    pending = not _loaded(today)
    summary = {k: v for k, v in res.items() if k != "stocks"} | {"today_pending": pending}
    finish(slot, summary, now, failed=pending or bool(res["failed"]))
    return {"fno_bhav": summary}


async def fno_participant_step(now: datetime, holidays: set[date] | None = None, **kw: Any) -> dict[str, Any]:
    """Claim tonight's participant-OI slot and fetch the missing recent days (one small CSV a night). `kw` goes to
    fnohist.participants.update (tests)."""
    from finresearch.monitor.disclosures import claim, finish

    bhav = due_slot(now, holidays)
    if bhav is None:
        return {}
    slot = bhav.replace("fno_bhav:", "fno_participant:")
    if not claim(slot, now):
        return {}
    today = to_ist(now).date()
    try:
        from finresearch.fnohist import participants

        res = await participants.update(today, **kw)
    except Exception as e:
        log.warning("F&O participant OI update failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"fno_participant": "failed"}
    finish(slot, res, now, failed=bool(res["failed"]) or not _participant_loaded(today))
    return {"fno_participant": res}


def _participant_loaded(day: date) -> bool:
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import FnoParticipantOi

    with session_scope() as s:
        return s.scalar(select(FnoParticipantOi.day).where(FnoParticipantOi.day == day).limit(1)) is not None


def _loaded(day: date) -> bool:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay

    with session_scope() as s:
        m = s.get(FnoBhavDay, day)
        return m is not None and m.status == "loaded"

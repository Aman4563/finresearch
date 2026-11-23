"""The monitor's nightly F&O bhavcopy update (#306, fnohist.store): once per trading-day evening from RUN_AFTER (IST).

NSE publishes the day's F&O bhavcopy in the evening (zip entry 17:56 IST on 30-Sep-2026, Last-Modified 18:59 and
19:17 IST on other days: adapters.nse_fno_bhav), so the job starts at 19:30. It runs the same resumable backfill over
the last LOOKBACK_DAYS days: normally one download (today's file), plus any weekend special session, any day missed
while the Mac was off, and any recent day whose file was late. When today's file is not there yet the run counts as
failed and is retried (monitor.disclosures.MAX_ATTEMPTS, 30 minutes apart); the next evening's run picks it up
otherwise. After loading, the day's derived IV row per underlying (fno_iv_eod) and the contract calendar are updated.
Weekends and NSE holidays are skipped; an unknown holiday list skips the run too (monitor.market_days fails closed).
Each night is claimed in `alert_eval_slot` ("fno_bhav:<day>"), so two monitor processes never run it twice.
After an outage longer than LOOKBACK_DAYS the window starts the day after the store's last loaded day, at most
catchup.MAX_CATCHUP_DAYS back; an older gap is reported, not fetched (monitor.catchup, #507). The participant and FII
files below do the same from their own last loaded day.

The same evening NSE's participant-wise F&O open interest and trading volume files (#317, #371, fnohist.participants)
are fetched in their own slot ("fno_participant:<day>"), so a failure of one never retries or fails the other. The
slot counts as failed (retried) until both of today's files are stored. NSE's FII derivative statistics workbook
(#371, fnohist.fii_stats) has its own slot too ("fii_stats:<day>"); without the owner's data-rights decision for it
(data_rights.PENDING) the night is recorded as skipped: no request, no retry.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
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
        from finresearch.monitor import catchup

        w = catchup.safely("fno_bhav", today, LOOKBACK_DAYS,
                           lambda s: catchup.fno_bhav(s, today, LOOKBACK_DAYS, holidays))  # fmt: skip
        catchup.report(
            "fno_bhav", "F&O bhavcopy", w, "uv run finresearch fno backfill --from {start} --to {end}"
        )
        catchup.polite("fno_bhav", w, kw)
        res = await store.backfill(w.start, today, today=today, **kw)
    except Exception as e:
        log.warning("F&O bhavcopy update failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"fno_bhav": "failed"}
    pending = not _loaded(today)
    summary = {k: v for k, v in res.items() if k != "stocks"} | {"today_pending": pending} | catchup.note(w)
    finish(slot, summary, now, failed=pending or bool(res["failed"]))
    return {"fno_bhav": summary}


async def fno_participant_step(now: datetime, holidays: set[date] | None = None, **kw: Any) -> dict[str, Any]:
    """Claim tonight's participant slot and fetch the missing recent days (two small CSVs a night: OI and volume).
    `kw` goes to fnohist.participants.update (tests)."""
    from finresearch.monitor.disclosures import claim, finish

    bhav = due_slot(now, holidays)
    if bhav is None:
        return {}
    slot = bhav.replace("fno_bhav:", "fno_participant:")
    if not claim(slot, now):
        return {}
    today = to_ist(now).date()
    try:
        from finresearch.adapters.nse_participant_oi import KINDS
        from finresearch.fnohist import participants
        from finresearch.monitor import catchup

        days = participants.LOOKBACK_DAYS
        w = catchup.safely("fno_participant", today, days,
                           lambda s: catchup.participant_files(s, today, days, KINDS, holidays))  # fmt: skip
        catchup.report("fno_participant", "F&O participant files", w,
                       "uv run finresearch fno participants --from {start} --to {end}")  # fmt: skip
        catchup.polite("fno_participant", w, kw)
        res = await participants.update(today, lookback=(today - w.start).days, holidays=holidays, **kw)
        res |= catchup.note(w)
    except Exception as e:
        log.warning("F&O participant OI update failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"fno_participant": "failed"}
    finish(slot, res, now, failed=bool(res["failed"]) or not _participant_loaded(today))
    return {"fno_participant": res}


async def fii_stats_step(now: datetime, holidays: set[date] | None = None, **kw: Any) -> dict[str, Any]:
    """Claim tonight's FII statistics slot and fetch the missing recent days (one small .xls a night). `kw` goes to
    fnohist.fii_stats.update (tests)."""
    from finresearch import data_rights
    from finresearch.monitor.disclosures import claim, finish

    bhav = due_slot(now, holidays)
    if bhav is None:
        return {}
    slot = bhav.replace("fno_bhav:", "fii_stats:")
    if not claim(slot, now):
        return {}
    today = to_ist(now).date()
    try:
        from finresearch.fnohist import fii_stats
        from finresearch.monitor import catchup

        data_rights.require(fii_stats.fs.DATASET)  # no decision: skipped below, before any catch-up report
        days = fii_stats.LOOKBACK_DAYS
        w = catchup.safely("fii_stats", today, days,
                           lambda s: catchup.participant_files(s, today, days, (fii_stats.SOURCE,), holidays))  # fmt: skip
        catchup.report("fii_stats", "FII derivative statistics", w,
                       "uv run finresearch fno fii-stats --from {start} --to {end}")  # fmt: skip
        catchup.polite("fii_stats", w, kw)
        res = await fii_stats.update(today, lookback=(today - w.start).days, holidays=holidays, **kw)
        res |= catchup.note(w)
    except data_rights.NoDecision as e:
        finish(slot, {"skipped": str(e)[:300]}, now, failed=False)
        return {"fii_stats": {"skipped": str(e)}}
    except Exception as e:
        log.warning("FII derivative statistics update failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"fii_stats": "failed"}
    finish(slot, res, now, failed=bool(res["failed"]) or "stopped" in res or not _fii_loaded(today))
    return {"fii_stats": res}


def _fii_loaded(day: date) -> bool:
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import FiiStats

    with session_scope() as s:
        return s.scalar(select(FiiStats.day).where(FiiStats.day == day).limit(1)) is not None


def _participant_loaded(day: date) -> bool:
    """Both participant files (OI and volume) are stored for `day`."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.fnohist.participants import TABLES

    with session_scope() as s:
        return all(s.scalar(select(t.day).where(t.day == day).limit(1)) is not None for t in TABLES.values())


def _loaded(day: date) -> bool:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay

    with session_scope() as s:
        m = s.get(FnoBhavDay, day)
        return m is not None and m.status == "loaded"

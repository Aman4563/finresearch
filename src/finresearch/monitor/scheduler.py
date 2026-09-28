"""The monitor loop: plan slots for active watches, claim due jobs and run them.

Each slot is a row with a unique key, inserted with ON CONFLICT DO NOTHING, and claimed with FOR UPDATE SKIP
LOCKED, so a check never runs twice for the same slot even with two monitor processes. Past slots older than the
grace period are not backfilled, and a planned slot still pending that long after its time is marked missed. A job whose information is not published yet (NotYet) or that hits a network
error is retried with a delay, up to MAX_ATTEMPTS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from finresearch.db import session_scope
from finresearch.db.models import MonitorJob, Watch
from finresearch.fincalc.dates import to_ist
from finresearch.monitor import jobs
from finresearch.monitor.plan import last_slot, plan, plan_stock

log = logging.getLogger(__name__)
GRACE = timedelta(hours=2)
STOCK_HORIZON_DAYS = 3
STALE = timedelta(minutes=15)
MAX_ATTEMPTS = {"listing": 8, "subscription": 3, "allotment": 3, "lockin": 3, "stock_daily": 3}
RETRY_DELAY = {"listing": timedelta(minutes=30), "subscription": timedelta(minutes=10)}  # network errors


def retry_at(job: MonitorJob, error: Exception, now: datetime) -> datetime:
    """When to try again. A stock that has not listed is checked again on the next exchange day at the same time
    (the calendar has no exchange holidays, so the expected listing date can be a day early)."""
    if job.kind == "listing" and isinstance(error, jobs.NotYet):
        from finresearch.fincalc.dates import next_business_day, to_ist

        due = to_ist(job.due_at)
        return due.replace(year=(d := next_business_day(to_ist(now).date())).year, month=d.month, day=d.day)
    return now + RETRY_DELAY.get(job.kind, timedelta(minutes=10))


def sync_slots(now: datetime) -> int:
    """Insert the not-yet-stored slots of every active watch; deactivate watches whose schedule has ended."""
    added = 0
    with session_scope() as s:
        for w in s.scalars(select(Watch).where(Watch.active.is_(True))):
            if w.kind == "stock":  # open-ended: plan only the next few days
                today = to_ist(now).date()
                slots = plan_stock(
                    w.nse_symbol, to_ist(now - GRACE).date(), today + timedelta(days=STOCK_HORIZON_DAYS)
                )
            else:
                slots = plan(
                    w.nse_symbol, w.open_date, w.close_date, w.allotment_date, w.listing_date, w.anchor_shares
                )
                if now > last_slot(slots):
                    w.active = False
                    continue
            for sl in slots:
                if sl.due_at < now - GRACE:
                    continue
                inserted = s.scalars(insert(MonitorJob).values(watch_id=w.id, kind=sl.kind, slot=sl.slot,
                                                               due_at=sl.due_at, params=sl.params, result={})
                                     .on_conflict_do_nothing(index_elements=["slot"])
                                     .returning(MonitorJob.id)).all()  # fmt: skip
                added += len(inserted)
    return added


def _recover_stale(now: datetime) -> None:
    with session_scope() as s:
        s.execute(update(MonitorJob).where(MonitorJob.status == "running", MonitorJob.started_at < now - STALE)
                  .values(status="pending"))  # fmt: skip


def _expire_missed(now: datetime) -> int:
    """A first attempt more than GRACE past its slot is marked missed: running it now would record "now" data
    under an old slot (for example six subscription checks at once after the monitor was down)."""
    with session_scope() as s:
        res = s.execute(update(MonitorJob).where(MonitorJob.status == "pending", MonitorJob.attempts == 0,
                                                 MonitorJob.due_at < now - GRACE)
                        .values(status="missed", finished_at=now).returning(MonitorJob.id))  # fmt: skip
        return len(res.all())


def _claim(now: datetime, limit: int = 10) -> list[int]:
    with session_scope() as s:
        rows = s.scalars(select(MonitorJob).where(MonitorJob.status == "pending", MonitorJob.due_at <= now)
                         .order_by(MonitorJob.due_at).limit(limit).with_for_update(skip_locked=True)).all()  # fmt: skip
        for j in rows:
            j.status, j.started_at, j.attempts = "running", now, j.attempts + 1
        return [j.id for j in rows]


async def run_job(job_id: int, deps: jobs.Deps, now: datetime) -> str:
    with session_scope() as s:
        job = s.get(MonitorJob, job_id)
        watch = s.get(Watch, job.watch_id)
        try:
            job.result = await jobs.HANDLERS[job.kind](s, job, watch, deps, now)
            job.status, job.error = "done", None
        except Exception as e:  # network errors and NotYet are retried; bugs end as failed after the attempts
            s.rollback()
            job = s.get(MonitorJob, job_id)
            watch = s.get(Watch, job.watch_id)
            job.error = f"{type(e).__name__}: {e}"[:500]
            if job.attempts < MAX_ATTEMPTS.get(job.kind, 3):
                job.status = "pending"
                job.due_at = retry_at(job, e, now)
            else:
                job.status = "failed"
                if not isinstance(e, jobs.NotYet) or job.kind != "subscription":
                    jobs.alert(s, watch, "monitor_error", f"{watch.nse_symbol}: the {job.kind} check failed "
                               f"{job.attempts} times ({job.error})", "warn")  # fmt: skip
        job.finished_at = now
        return job.status


async def tick(deps: jobs.Deps, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    _recover_stale(now)
    added = sync_slots(now)
    missed = _expire_missed(now)
    done = failed = retried = 0
    for jid in _claim(now):
        st = await run_job(jid, deps, now)
        done += st == "done"
        failed += st == "failed"
        retried += st == "pending"
    return {"added": added, "done": done, "failed": failed, "retried": retried, "missed": missed}


async def run_forever(
    deps: jobs.Deps | None = None, interval_s: float = 60.0, stop: asyncio.Event | None = None
):
    deps = deps or jobs.Deps.live()
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            stats = await tick(deps)
            if any(stats.values()):
                log.info("monitor tick: %s", stats)
        except Exception:
            log.exception("monitor tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)

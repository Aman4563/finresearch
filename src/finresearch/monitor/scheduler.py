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
TICK_S = 60.0  # the monitor loop's pass interval
RETRY_DELAY = {"listing": timedelta(minutes=30), "subscription": timedelta(minutes=10)}  # network errors


def retry_at(job: MonitorJob, error: Exception, now: datetime) -> datetime:
    """When to try again. A stock that has not listed is checked again on the next trading day at the same time."""
    if job.kind == "listing" and isinstance(error, jobs.NotYet):
        from finresearch.adapters.nse_holidays import trading_holidays
        from finresearch.fincalc.dates import next_business_day, to_ist

        due = to_ist(job.due_at)
        d = next_business_day(to_ist(now).date(), trading_holidays())
        return due.replace(year=d.year, month=d.month, day=d.day)
    return now + RETRY_DELAY.get(job.kind, timedelta(minutes=10))


def watch_windows():
    """The profile's watch windows (check times); the defaults when the profile cannot be read."""
    from finresearch.suggest.profile import WatchWindows

    try:
        from finresearch.suggest.advisor import load_profile

        with session_scope() as s:
            return load_profile(s).preferences.watch
    except Exception:
        log.warning("could not read the profile's watch windows; using the defaults", exc_info=True)
        return WatchWindows()


def sync_slots(now: datetime) -> int:
    """Insert the not-yet-stored slots of every active watch; deactivate watches whose schedule has ended. Check
    times come from the profile's watch windows; changing them re-plans pending checks (the old slots are cancelled
    below because they are no longer planned)."""
    added = 0
    ww = watch_windows()
    sub_times, stock_at = ww.subscription_times(), ww.stock_time()
    with session_scope() as s:
        for w in s.scalars(select(Watch).where(Watch.active.is_(True))):
            if w.kind == "stock":  # open-ended: plan only the next few days
                today = to_ist(now).date()
                slots = plan_stock(w.key, to_ist(now - GRACE).date(),
                                   today + timedelta(days=STOCK_HORIZON_DAYS), at=stock_at)  # fmt: skip
            else:
                listed = frozenset(
                    x for x in ("open", "close") if (w.meta or {}).get(f"listing_{x}") is not None
                )
                slots = plan(w.nse_symbol, w.open_date, w.close_date, w.allotment_date, w.listing_date,
                             w.anchor_shares, listed=listed, subscription_times=sub_times)  # fmt: skip
                if now > last_slot(slots):
                    w.active = False
                    continue
            planned = {sl.slot for sl in slots}
            # dates can move (holiday lists refreshed, NSE confirms a listing date): pending checks for slots that are
            # no longer planned are cancelled instead of running on the wrong day
            s.execute(update(MonitorJob).where(MonitorJob.watch_id == w.id, MonitorJob.status == "pending",
                                               MonitorJob.attempts == 0, MonitorJob.slot.not_in(planned),
                                               MonitorJob.due_at > now)
                      .values(status="cancelled", finished_at=now))  # fmt: skip
            # a listing check keyed to the old expected date may already be retrying: its event now has a new slot
            s.execute(update(MonitorJob).where(MonitorJob.watch_id == w.id, MonitorJob.status == "pending",
                                               MonitorJob.kind == "listing", MonitorJob.slot.not_in(planned))
                      .values(status="cancelled", finished_at=now))  # fmt: skip
            # a slot whose time changed (the profile's watch windows) keeps its key: move its pending first attempt
            due_by_slot = {sl.slot: sl.due_at for sl in slots}
            for j in s.scalars(select(MonitorJob).where(MonitorJob.watch_id == w.id, MonitorJob.status == "pending",
                                                        MonitorJob.attempts == 0,
                                                        MonitorJob.slot.in_(list(due_by_slot)))):  # fmt: skip
                if j.due_at != due_by_slot[j.slot]:
                    j.due_at = due_by_slot[j.slot]
            for sl in slots:
                if sl.due_at < now - GRACE:
                    continue
                ins = insert(MonitorJob).values(watch_id=w.id, kind=sl.kind, slot=sl.slot, due_at=sl.due_at,
                                                params=sl.params, result={})  # fmt: skip
                # a slot cancelled by a stop (or a date move) is planned again once the watch is back
                inserted = s.scalars(ins.on_conflict_do_update(
                    index_elements=["slot"], where=MonitorJob.status == "cancelled",
                    set_={"watch_id": w.id, "kind": sl.kind, "due_at": sl.due_at, "params": sl.params,
                          "status": "pending", "attempts": 0, "error": None, "result": {}, "started_at": None,
                          "finished_at": None},
                ).returning(MonitorJob.id)).all()  # fmt: skip
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
        rows = s.scalars(select(MonitorJob).join(Watch, Watch.id == MonitorJob.watch_id)
                         .where(MonitorJob.status == "pending", MonitorJob.due_at <= now, Watch.active.is_(True))
                         .order_by(MonitorJob.due_at).limit(limit)
                         .with_for_update(skip_locked=True, of=MonitorJob)).all()  # fmt: skip
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
                    jobs.alert(s, watch, "monitor_error", f"{watch.label}: the {job.kind} check failed "
                               f"{job.attempts} times ({job.error})", "warn")  # fmt: skip
        job.finished_at = now
        return job.status


_HOLIDAYS_CHECKED: dict[str, float] = {}


async def _refresh_holidays(deps: jobs.Deps) -> None:
    """Keep NSE's holiday lists fresh (at most one attempt a day; failures keep the cached lists). While the current
    year's list is missing altogether a failed attempt is retried within the hour instead of the next day: the
    after-close market jobs (stock peers, disclosures, IV history) fail closed and stay paused, with one warning
    alert a day (monitor.market_days), and the IPO watch plan still treats unknown holidays as trading days."""
    import time

    from finresearch.adapters.nse_holidays import cached_years
    from finresearch.fincalc.dates import today_ist

    every = 86400 if today_ist().year in cached_years() else 3600
    if _HOLIDAYS_CHECKED.get("at", 0) > time.time() - every:
        return
    _HOLIDAYS_CHECKED["at"] = time.time()
    try:
        from finresearch.adapters.nse_holidays import refresh_holidays

        await refresh_holidays(fetch=deps.holidays)
    except Exception:
        log.warning("could not refresh NSE holidays; using the cached lists", exc_info=True)


async def _record_iv(deps: jobs.Deps, now: datetime) -> None:
    """Daily ATM IV after the close (monitor.iv); failures are logged and retried there, never break the tick."""
    try:
        from finresearch.monitor.iv import record_iv

        res = await record_iv(deps.fno, now)
        if res.get("recorded"):
            log.info("IV history recorded: %s", res["recorded"])
    except Exception:
        log.warning("could not record IV history", exc_info=True)


async def _archive_intraday(deps: jobs.Deps, now: datetime) -> None:
    """Store today's 1-minute series after the close (monitor.intraday); failures never break the tick."""
    try:
        from finresearch.monitor.intraday import archive_after_close

        res = await archive_after_close(deps.intraday, now)
        if res.get("archived"):
            log.info("intraday archived: %s", res["archived"])
    except Exception:
        log.warning("could not archive intraday series", exc_info=True)


async def _archive_data(deps: jobs.Deps, now: datetime) -> None:
    """The daily validation archive (monitor.archive); failures are logged and retried there, never break the tick."""
    try:
        from finresearch.monitor.archive import ArchiveSources, archive_step

        res = await archive_step(now, ArchiveSources(fno=deps.fno))
        if res.get("archived"):
            log.info("data archived: %s", res["archived"])
    except Exception:
        log.warning("could not archive validation data", exc_info=True)


FORECAST_EVERY = timedelta(hours=1)
_FORECASTS_CHECKED: dict[str, datetime] = {}


async def forecast_step(deps: jobs.Deps, now: datetime) -> dict[str, int]:
    """The forecast ledger's daily work (signals.ledger): log verdicts of finished runs that have no forecast yet, then
    resolve the forecasts whose date has passed. At most once an hour; each forecast is itself checked at most every
    few hours and only after the close on its date, so this makes a handful of NSE requests a day at most."""
    last = _FORECASTS_CHECKED.get("at")
    if last is not None and now - last < FORECAST_EVERY:
        return {}
    _FORECASTS_CHECKED["at"] = now
    from finresearch.signals import ledger

    try:
        with session_scope() as s:
            logged = len(ledger.backfill_runs(s))
        return {"forecasts_logged": logged, **await ledger.resolve_due(deps, now)}
    except Exception:
        log.warning("forecast ledger step failed", exc_info=True)
        return {}


async def alerts_step(now: datetime) -> dict[str, int]:
    """Alert rules for every asset (finresearch.alerts.engine) and phone delivery (monitor.notify). Failures are
    logged and retried on the next tick; they never break the tick."""
    out: dict[str, int] = {}
    try:
        from finresearch.alerts.engine import rules_step

        res = await rules_step(now)
        out["rules_fired"] = sum(v.get("fired", 0) for v in res.values())
    except Exception:
        log.warning("alert-rule evaluation failed", exc_info=True)
    try:
        from finresearch.monitor.notify import deliver_due

        res = await deliver_due(now)
        out["notified"] = res["sent"]
    except Exception:
        log.warning("alert delivery failed", exc_info=True)
    return {k: v for k, v in out.items() if v}


def journal_step(now: datetime) -> dict[str, int]:
    """Journal drafts for newly imported trades and review reminders (portfolio.journal): database only, so inline.
    Runs before alerts_step, whose delivery pass forwards the reminders."""
    try:
        from finresearch.db import session_scope
        from finresearch.portfolio.journal import review_step

        with session_scope() as s:
            res = review_step(s, now)
        return {f"journal_{k}": v for k, v in res.items() if v}
    except Exception:
        log.warning("journal step failed; it is retried on the next tick", exc_info=True)
        return {}


_BACKGROUND: dict[str, asyncio.Task] = {}


def _spawn_portfolio(deps: jobs.Deps, now: datetime) -> None:
    """Start the daily portfolio pass (monitor.portfolio_daily) in the background when one is due: it can take a few
    minutes (quotes, signals, events at polite rates) and must not hold up the IPO checks in this tick. At most one
    runs at a time; failures are logged and retried there."""
    from finresearch.monitor.portfolio_daily import portfolio_step

    t = _BACKGROUND.get("portfolio")
    if t is not None and not t.done():
        return

    async def run() -> None:
        try:
            res = await portfolio_step(deps, now)
            if res:
                log.info("portfolio daily pass: %s", res)
        except Exception:
            log.warning("portfolio daily pass failed", exc_info=True)

    _BACKGROUND["portfolio"] = asyncio.create_task(run())


def _spawn_disclosures(now: datetime) -> None:
    """Start the daily disclosure refresh (monitor.disclosures) in the background when a pass is due: the evening
    pass reads a few requests per held or watched stock at polite rates. At most one runs at a time."""
    from finresearch.monitor.disclosures import disclosures_step, due_passes

    t = _BACKGROUND.get("disclosures")
    if (t is not None and not t.done()) or not due_passes(now):
        return

    async def run() -> None:
        try:
            res = await disclosures_step(now)
            if res:
                log.info(
                    "disclosure refresh: %s", {k: v if isinstance(v, str) else "done" for k, v in res.items()}
                )
        except Exception:
            log.warning("disclosure refresh failed", exc_info=True)

    _BACKGROUND["disclosures"] = asyncio.create_task(run())


def _spawn_fund_ranks(now: datetime) -> None:
    """Start the daily fund category ranking (monitor.fund_ranks) in the background: the first run reads up to 61
    AMFI month-end snapshots at one request a second. At most one runs at a time."""
    from finresearch.monitor.fund_ranks import due_slot, fund_ranks_step

    t = _BACKGROUND.get("fund_ranks")
    if (t is not None and not t.done()) or due_slot(now) is None:
        return

    async def run() -> None:
        try:
            res = await fund_ranks_step(now)
            if res:
                log.info("fund category ranking: %s", res)
        except Exception:
            log.warning("fund category ranking failed", exc_info=True)

    _BACKGROUND["fund_ranks"] = asyncio.create_task(run())


def _spawn_lookthrough(now: datetime) -> None:
    """Start the monthly fund look-through fetch (monitor.lookthrough_fetch) in the background on IST days 11-25: a
    few polite requests per fund house, none once the month's portfolios are stored. At most one runs at a time."""
    from finresearch.monitor.lookthrough_fetch import due_slot, lookthrough_step

    t = _BACKGROUND.get("lookthrough")
    if (t is not None and not t.done()) or due_slot(now) is None:
        return

    async def run() -> None:
        try:
            res = await lookthrough_step(now)
            if res:
                log.info("fund look-through fetch: %s", res)
        except Exception:
            log.warning("fund look-through fetch failed", exc_info=True)

    _BACKGROUND["lookthrough"] = asyncio.create_task(run())


def _spawn_stock_peers(now: datetime) -> None:
    """Start the nightly stock peer build (monitor.stock_peers) in the background: ~1,000 polite NSE requests for the
    NIFTY 500, plus a bounded few hundred when held/watched stocks fall outside it (signals.stock_peers). At most one
    runs at a time."""
    from finresearch.monitor.stock_peers import due_slot, stock_peers_step

    t = _BACKGROUND.get("stock_peers")
    if (t is not None and not t.done()) or due_slot(now) is None:
        return

    async def run() -> None:
        try:
            res = await stock_peers_step(now)
            if res:
                log.info("stock peer build: %s", res)
        except Exception:
            log.warning("stock peer build failed", exc_info=True)

    _BACKGROUND["stock_peers"] = asyncio.create_task(run())


async def drain() -> None:
    """Wait for background work started by `tick` (a one-shot `finresearch monitor tick` calls this)."""
    for t in list(_BACKGROUND.values()):
        with contextlib.suppress(Exception):
            await t


def brief_step(now: datetime) -> dict[str, int]:
    """The 08:30 brief and the Sunday digest (monitor.digest): database only, so it runs inline."""
    try:
        from finresearch.monitor.digest import brief_step as step

        return step(now)
    except Exception:
        log.warning("morning brief failed; it is retried on the next tick", exc_info=True)
        return {}


async def tick(deps: jobs.Deps, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    if deps.portfolio_daily:
        _spawn_portfolio(deps, now)
    if deps.disclosures:
        _spawn_disclosures(now)
    if deps.fund_ranks:
        _spawn_fund_ranks(now)
    if deps.stock_peers:
        _spawn_stock_peers(now)
    if deps.lookthrough:
        _spawn_lookthrough(now)
    out_brief = brief_step(now) if deps.brief else {}
    if deps.holidays is not None or deps.live_holidays:
        await _refresh_holidays(deps)
    if deps.fno is not None:
        await _record_iv(deps, now)
    if deps.intraday is not None:
        await _archive_intraday(deps, now)
    if deps.archive:
        await _archive_data(deps, now)
    _recover_stale(now)
    added = sync_slots(now)
    missed = _expire_missed(now)
    done = failed = retried = 0
    for jid in _claim(now):
        st = await run_job(jid, deps, now)
        done += st == "done"
        failed += st == "failed"
        retried += st == "pending"
    out = {"added": added, "done": done, "failed": failed, "retried": retried, "missed": missed}
    if deps.forecasts:
        out |= {k: v for k, v in (await forecast_step(deps, now)).items() if k in ("resolved", "void")}
    out |= journal_step(now)
    out |= await alerts_step(now)
    try:  # broker syncs after the close and the statement inbox (portfolio.connectors.sync)
        from finresearch.portfolio.connectors.sync import connections_step

        out |= await connections_step(now)
    except Exception:
        log.warning("broker connection step failed; it is retried on the next tick", exc_info=True)
    out |= {f"{k}_sent": v for k, v in out_brief.items()}
    try:
        res = await jobs.archive_open_books(deps, now)
        out["archived"] = len(res["archived"]) if res else 0
    except Exception:
        log.warning("subscription archive pass failed; it is retried on the next tick", exc_info=True)
    return out


async def run_forever(
    deps: jobs.Deps | None = None, interval_s: float = TICK_S, stop: asyncio.Event | None = None
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


def schedule_json(ww=None, *, running: bool | None = None) -> dict:
    """What the monitor does and when, from the live constants and the profile's watch windows: the Monitor page's
    "How monitoring works" box reads this instead of hardcoding times (all IST)."""
    import inspect

    from finresearch.api.live import BIDDING_HOURS, EQUITY_HOURS
    from finresearch.fincalc.ipo import lock_in_schedule
    from finresearch.monitor import digest as dg
    from finresearch.monitor import intraday, iv
    from finresearch.monitor import lookthrough_fetch as ltf
    from finresearch.monitor import portfolio_daily as pd
    from finresearch.monitor.plan import ALLOTMENT_TIME, LISTING_TIMES, LOCKIN_TIME
    from finresearch.signals.ledger import READY_AFTER_IST
    from finresearch.suggest.profile import IPO_FINAL_CHECK

    ww = ww or watch_windows()
    hm = lambda t: f"{t[0]:02d}:{t[1]:02d}"  # noqa: E731
    return {
        "running": running,
        "tick_s": int(TICK_S),
        "grace_hours": GRACE.total_seconds() / 3600,
        "ipo": {"check_times": list(ww.ipo_check_times), "final_check": IPO_FINAL_CHECK,
                "bidding_hours": [hm((BIDDING_HOURS[0].hour, BIDDING_HOURS[0].minute)),
                                  hm((BIDDING_HOURS[1].hour, BIDDING_HOURS[1].minute))],
                "allotment_time": hm(ALLOTMENT_TIME), "listing_times": {w: hm((h, m)) for h, m, w in LISTING_TIMES},
                "lockin_time": hm(LOCKIN_TIME), "anchor_lockin_days": list(inspect.signature(lock_in_schedule).parameters["anchor_days"].default),
                "archive_times": [hm(t) for t in jobs.ARCHIVE_TIMES], "archive_window_min": jobs.ARCHIVE_WINDOW_MIN,
                "listing_max_attempts": MAX_ATTEMPTS["listing"]},
        "stock": {"daily_time": ww.stock_daily_time, "horizon_days": STOCK_HORIZON_DAYS},
        "quiet": {"start": ww.quiet_start, "end": ww.quiet_end},
        "intraday": {"from": hm(intraday.START), "indices": list(intraday.ARCHIVE_INDICES),
                     "max_tries": intraday.MAX_TRIES, "retry_min": intraday.RETRY_S // 60},
        "iv": {"from": hm(iv.START), "indices": list(iv.INDEX_SYMBOLS)},
        "forecasts": {"after": hm(READY_AFTER_IST), "every_min": int(FORECAST_EVERY.total_seconds() // 60)},
        "portfolio": {"close_pass": hm((pd.close_time(ww.stock_time()).hour, pd.close_time(ww.stock_time()).minute)),
                      "nav_pass": hm((pd.NAV_AT.hour, pd.NAV_AT.minute)), "max_instruments": pd.MAX_INSTRUMENTS,
                      "brief": hm((dg.BRIEF_AT.hour, dg.BRIEF_AT.minute)),
                      "digest": hm((dg.DIGEST_AT.hour, dg.DIGEST_AT.minute)), "digest_day": "Sunday"},
        "lookthrough": {"from_day": ltf.FIRST_DAY, "to_day": ltf.LAST_DAY, "after": hm((ltf.RUN_AFTER.hour,
                                                                               ltf.RUN_AFTER.minute)),
                        "sebi_days": 10},
        "equity_hours": [hm((EQUITY_HOURS[0].hour, EQUITY_HOURS[0].minute)),
                         hm((EQUITY_HOURS[1].hour, EQUITY_HOURS[1].minute))],
    }  # fmt: skip

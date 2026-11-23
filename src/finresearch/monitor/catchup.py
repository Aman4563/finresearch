"""Catch-up after an outage (#507): how far back tonight's nightly EOD job reads.

Each nightly job (monitor.fno_bhav, index_eod, equity_eod, span) runs its store's resumable backfill over a short
lookback (4 to 10 days). When the app was down for longer (2-Nov to 23-Nov-2026: three weeks unnoticed), the days
before that window were never fetched again. `window` moves tonight's start back to the day after the store's own
last loaded day, and to the oldest day the store's own plan still wants inside the bound (a day that failed in the
middle of a catch-up, or a hole left by a partial run), so an outage heals on the first evening the job runs again.

Bounded: never further back than MAX_CATCHUP_DAYS [U] (45 calendar days, about 31 sessions: the issue's proposal; one
evening's worth of small files, and the 30 sessions of SPAN zips the margin engine keeps). SPAN_MAX_CATCHUP_DAYS [U]
is smaller because one SPAN file is ~10 MB and fnohist.span calls daily history "a disk and request decision". A gap
that reaches beyond the bound is reported, never silently skipped: `uncovered` names its first and last session, the
job's result carries it, a warning is logged and one alert per gap says which command fills it (`report`). That
covers the sessions between the last loaded day and the bound, and a hole behind newer loaded days as far back as
REPORT_DAYS (an older build's short lookback filled only the newest days of an outage and left such a hole).

What stays as it was: the job still runs once per trading-day evening from its RUN_AFTER (19:00 to 19:30 IST), so a
catch-up never downloads in market hours; the same backfill does the work, with the shared NSE budget, its stops
after refusals or repeated failures, and the manual backfill's pause between downloads (PAUSE_S) while a catch-up is
on; an empty store keeps the normal lookback (its history is the CLI backfill's job, not an outage); the lookback
never shrinks.

Sessions here are weekdays that are not known NSE trading holidays. An unknown holiday list counts every weekday as
a session: asking for a holiday's file costs one 404, and hiding a real gap would lose data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

MAX_CATCHUP_DAYS = 45  # [U] calendar days; proposed in #507, not measured against NSE's tolerance
SPAN_MAX_CATCHUP_DAYS = 14  # [U] calendar days: at most ~10 SPAN zips (~100 MB) in one evening
REPORT_DAYS = (
    90  # how far back a hole behind newer loaded days is still looked for and reported (never fetched)
)
ALERT_KIND = "catchup_gap"
# seconds between downloads while a catch-up is on: each dataset's manual backfill default (`finresearch fno
# backfill --delay`, `index backfill`, `equity backfill`, `fno span backfill`, `fno participants`, `fno fii-stats`)
PAUSE_S = {"fno_bhav": 0.5, "index_eod": 2.0, "equity_eod": 2.0, "span": 5.0, "fno_participant": 2.0,
           "fii_stats": 3.0}  # fmt: skip
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Window:
    """Tonight's backfill start, the job's normal start, and the sessions older than the bound that tonight's run
    will not fetch (first, last, count), or None."""

    start: date
    normal: date
    uncovered: tuple[date, date, int] | None = None
    error: str | None = (
        None  # the window could not be worked out (`safely`): the normal lookback, gaps unknown
    )

    @property
    def extended(self) -> bool:
        return self.start < self.normal

    def summary(self) -> dict[str, Any] | None:
        """For the job's result: None on an ordinary night."""
        if not self.extended and self.uncovered is None and self.error is None:
            return None
        out: dict[str, Any] = {"from": self.start.isoformat(), "days": (self.normal - self.start).days}
        if self.error is not None:
            out["error"] = self.error
        if self.uncovered is not None:
            a, b, n = self.uncovered
            out["uncovered"] = {"from": a.isoformat(), "to": b.isoformat(), "sessions": n}
        return out


def is_session(day: date, holidays: Collection[date] | None) -> bool:
    return day.weekday() < 5 and (holidays is None or day not in holidays)


def window(today: date, lookback: int, last_loaded: date | None, *, missing: Iterable[date] = (),
           older: Iterable[date] = (), bound: int = MAX_CATCHUP_DAYS,
           holidays: Collection[date] | None = None) -> Window:  # fmt: skip
    """Pure arithmetic. `last_loaded`: the store's newest loaded day (None: an empty store). `missing`: days the
    store's own plan would still fetch (those outside [today - bound, today - lookback) are ignored). `older`: days
    that plan still wants that are older than the bound (a hole behind newer loaded days): reported with the
    sessions between `last_loaded` and the bound, never fetched. `holidays`: the known trading holidays, None when
    unknown."""
    normal = today - timedelta(days=lookback)
    if last_loaded is None:
        return Window(normal, normal)
    floor = min(today - timedelta(days=bound), normal)
    want = min([normal, last_loaded + DAY, *(d for d in missing if floor <= d < normal)])
    gap = {d for d in _days(last_loaded + DAY, floor - DAY) if is_session(d, holidays)}
    gap |= {d for d in older if d < floor and is_session(d, holidays)}
    return Window(max(want, floor), normal, (min(gap), max(gap), len(gap)) if gap else None)


def _days(a: date, b: date) -> Iterable[date]:
    d = a
    while d <= b:
        yield d
        d += DAY


def merge(*ws: Window) -> Window:
    """One window for a job that loads several files a day (participant OI and volume): the earliest start, and the
    widest uncovered range (its session count is the largest of the parts)."""
    gaps = [w.uncovered for w in ws if w.uncovered is not None]
    unc = (min(g[0] for g in gaps), max(g[1] for g in gaps), max(g[2] for g in gaps)) if gaps else None
    return Window(min(w.start for w in ws), min(w.normal for w in ws), unc)


# ---------------------------------------------------------------------------------------------- the stores
def _known_holidays(a: date, b: date, holidays: set[date] | None) -> set[date]:
    """The caller's list, else every cached year's between a and b (an uncached year adds none: its weekdays count
    as sessions)."""
    if holidays is not None:
        return holidays
    from finresearch.adapters.nse_holidays import known_trading_holidays

    out: set[date] = set()
    for year in range(a.year, b.year + 1):
        out |= known_trading_holidays(date(year, 1, 1)) or set()
    return out


def for_store(s: Any, today: date, lookback: int, day_col: Any, where: tuple[Any, ...],
              plan: Callable[[date, date], list[date]], *, bound: int = MAX_CATCHUP_DAYS,
              holidays: set[date] | None = None, since: date | None = None) -> Window:  # fmt: skip
    """`window` for one store: `day_col` (filtered by `where`) holds its loaded days and `plan(start, end)` is its
    own list of days still to fetch, asked once from REPORT_DAYS back: the days inside the bound move the start, the
    older ones are reported. Days before the store's first loaded day are its history, never a gap; so are days
    before `since`, the first day of the store's daily coverage when that is later (SPAN)."""
    from sqlalchemy import func, select

    first, last = s.execute(select(func.min(day_col), func.max(day_col)).where(*where)).one()
    normal, floor = today - timedelta(days=lookback), today - timedelta(days=bound)
    horizon = min(today - timedelta(days=REPORT_DAYS), floor)
    if since is not None and last is not None:
        last = max(last, since - DAY)
    wanted: list[date] = []
    if first is not None and horizon < normal:
        wanted = [d for d in plan(horizon, normal - DAY) if d > first and (since is None or d >= since)]
    return window(today, lookback, last, missing=[d for d in wanted if d >= floor],
                  older=[d for d in wanted if d < floor], bound=bound,
                  holidays=_known_holidays(horizon - timedelta(days=366), today, holidays))  # fmt: skip


def fno_bhav(s: Any, today: date, lookback: int, holidays: set[date] | None = None) -> Window:
    from finresearch.db.models import FnoBhavDay as D
    from finresearch.fnohist import store

    # no stocks: a newly tracked stock re-loads the normal lookback only (fnohist.store), as before
    return for_store(s, today, lookback, D.day, (D.status == "loaded",),
                     lambda a, b: store.plan_days(s, a, b, today, [], holidays), holidays=holidays)  # fmt: skip


def index_eod(s: Any, today: date, lookback: int, holidays: set[date] | None = None) -> Window:
    from finresearch.db.models import IndexEodDay as D
    from finresearch.refseries import index_eod as store

    return for_store(s, today, lookback, D.day, (D.status == "loaded",),
                     lambda a, b: store.plan_days(s, a, b, today, holidays), holidays=holidays)  # fmt: skip


def equity_eod(s: Any, today: date, lookback: int, holidays: set[date] | None = None) -> Window:
    from finresearch.db.models import EquityEodDay as D
    from finresearch.refseries import equity_eod as store

    return for_store(s, today, lookback, D.day, (D.status == "loaded",),
                     lambda a, b: store.plan_days(s, a, b, today, holidays), holidays=holidays)  # fmt: skip


def span(s: Any, today: date, lookback: int, holidays: set[date] | None = None) -> Window:
    """SPAN's history before the nightly job is one sampled file a month (fnohist.span), so its daily coverage
    starts on the first evening the nightly job ran: the oldest "span:<day>" slot (tonight's, on its first night)."""
    from sqlalchemy import func, select

    from finresearch.db.models import AlertEvalSlot
    from finresearch.db.models import SpanDay as D
    from finresearch.fnohist import span as store

    oldest = s.scalar(select(func.min(AlertEvalSlot.slot)).where(AlertEvalSlot.slot.like("span:%")))
    since = date.fromisoformat(oldest.split(":", 1)[1]) if oldest else today
    return for_store(s, today, lookback, D.day, (D.status == "loaded",),
                     lambda a, b: store.plan_days(s, a, b, today, holidays, monthly=False),
                     bound=SPAN_MAX_CATCHUP_DAYS, holidays=holidays, since=since)  # fmt: skip


def participant_files(s: Any, today: date, lookback: int, kinds: Iterable[Any],
                      holidays: set[date] | None = None) -> Window:  # fmt: skip
    """The participant OI and volume files (or the FII statistics Source): each file's own window, merged, so the
    file that is further behind sets the start."""
    from finresearch.fnohist import participants as store

    def one(kind: Any) -> Window:
        table = store._source(kind).table
        return for_store(s, today, lookback, table.day, (),
                         lambda a, b: store.plan_days(s, kind, a, b, today, holidays), holidays=holidays)  # fmt: skip

    return merge(*(one(k) for k in kinds))


def safely(job: str, today: date, lookback: int, compute: Callable[[Any], Window]) -> Window:
    """`compute(session)` in its own session. If it fails, tonight's job still runs over its normal lookback (a
    fault here must never cost tonight's file), the failure is logged, and the job's result says the catch-up could
    not be worked out: unknown, not "nothing was missed"."""
    from finresearch.db import session_scope

    try:
        with session_scope() as s:
            return compute(s)
    except Exception as e:
        log.warning("catch-up window for %s could not be worked out; tonight uses the normal lookback", job,
                    exc_info=True)  # fmt: skip
        normal = today - timedelta(days=lookback)
        return Window(normal, normal, error=f"{type(e).__name__}: {e}"[:200])


# ---------------------------------------------------------------------------------------------- reporting
def report(job: str, label: str, w: Window, command: str) -> None:
    """Say that sessions older than the bound were not fetched: a warning in the log every night it holds, and one
    alert per job and gap (claimed in alert_eval_slot, so two monitor processes raise it once). A failure to raise
    the alert never breaks the job."""
    if w.uncovered is None:
        return
    a, b, n = w.uncovered
    fix = command.format(start=a.isoformat(), end=b.isoformat())
    msg = (f"{label}: {n} session(s) from {a:%d-%b-%Y} to {b:%d-%b-%Y} were missed and are older than the nightly "
           f"catch-up reaches (it starts at {w.start:%d-%b-%Y}). They are not fetched automatically; outside market "
           f"hours run: {fix}")  # fmt: skip
    log.warning("%s", msg)
    try:
        from sqlalchemy.dialects.postgresql import insert

        from finresearch.db import session_scope
        from finresearch.db.models import Alert, AlertEvalSlot

        with session_scope() as s:
            got = s.execute(insert(AlertEvalSlot).values(slot=f"{ALERT_KIND}:{job}:{a.isoformat()}",
                                                         result={"status": "done", "to": b.isoformat()})
                            .on_conflict_do_nothing(index_elements=["slot"]).returning(AlertEvalSlot.slot)).first()  # fmt: skip
            if got is not None:
                s.add(Alert(kind=ALERT_KIND, level="warn", message=msg,
                            data={"job": job, "from": a.isoformat(), "to": b.isoformat(), "sessions": n,
                                  "command": fix}))  # fmt: skip
    except Exception:
        log.warning("could not raise the catch-up gap alert for %s", job, exc_info=True)


def polite(job: str, w: Window, kw: dict[str, Any]) -> None:
    """While a catch-up is on, pause between downloads as the dataset's manual backfill does (a test's own `pause`
    wins)."""
    if w.extended:
        kw.setdefault("pause", PAUSE_S[job])


def note(w: Window) -> dict[str, Any]:
    """{"catch_up": ...} for the job's result, {} on an ordinary night."""
    got = w.summary()
    return {} if got is None else {"catch_up": got}

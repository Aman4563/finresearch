"""One data-quality pass (#368): per dataset, freshness against its SLA and the NSE holiday calendar, then the content
checks of the expected session (or, when it is missing, of the last loaded one); every result upserted into dq_check
(one row per dataset, day and check: a re-run replaces it) and one dataset_status row per dataset.

freshness lag = sessions between the last loaded day and the last session whose file should be out (dq.stats). SLAs
are the issue's (IST): bhavcopy 20:30, participant OI 20:00, index closes 18:30. SPAN and the derived IV ride with the
bhavcopy (20:30); the forward quotes' last snapshot is 15:25 and the intraday archive runs from 15:45, so 16:00. An
unknown holiday list leaves freshness unknown (fails closed, like monitor.market_days), never ok.

dataset status: fail when a critical check failed; warn when any other check failed or warned; unknown when a critical
check could not be evaluated; else ok. Non-critical unknowns (no lineage for an old load) do not make a dataset
unknown.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db import models as M
from finresearch.dq import fno, misc, ref, stats
from finresearch.dq.base import Check
from finresearch.fincalc.dates import IST, to_ist

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Dataset:
    name: str
    sla: time
    last: Callable[[Session], date | None]
    checks: Callable[[Session, date], list[Check]]
    critical: bool = True


def _max(col, *where) -> Callable[[Session], date | None]:
    return lambda s: s.scalar(select(func.max(col)).where(*where))


def _last_quote(s: Session) -> date | None:
    at = s.scalar(select(func.max(M.OptionQuote.snapshot_at)))
    return None if at is None else at.astimezone(IST).date()


DATASETS: tuple[Dataset, ...] = (
    Dataset(
        "fno_bhav", time(20, 30), _max(M.FnoBhavDay.day, M.FnoBhavDay.status == "loaded"), fno.bhav_checks
    ),
    Dataset("fno_iv_eod", time(20, 30), _max(M.FnoIvEod.day, M.FnoIvEod.symbol == "NIFTY"), ref.iv_checks),
    Dataset("fno_participant_oi", time(20, 0), _max(M.FnoParticipantOi.day), ref.participant_checks),
    Dataset(
        "index_eod", time(18, 30), _max(M.IndexEodDay.day, M.IndexEodDay.status == "loaded"), ref.index_checks
    ),
    Dataset("span", time(20, 30), _max(M.SpanDay.day, M.SpanDay.status == "loaded"), ref.span_checks),
    Dataset("option_quote", time(15, 30), _last_quote, misc.quote_checks, critical=False),
    Dataset(
        "intraday_series",
        time(16, 0),
        _max(
            M.IntradaySeriesRow.day,
            M.IntradaySeriesRow.kind == "index",
            M.IntradaySeriesRow.complete.is_(True),
        ),
        misc.intraday_checks,
        critical=False,
    ),
)


def _holidays(day: date, holidays: set[date] | None) -> set[date] | None:
    if holidays is not None:
        return holidays
    from finresearch.adapters.nse_holidays import known_trading_holidays

    return known_trading_holidays(day)


def freshness(
    ds: Dataset, last: date | None, now: datetime, hol: set[date] | None
) -> tuple[Check, date | None]:
    """The freshness check and the session it expected (None when the holiday list is unknown)."""
    if hol is None:
        return Check(
            ds.name, "freshness", "unknown", note="NSE holiday list not loaded", critical=ds.critical
        ), None
    want = stats.expected_day(now, ds.sla, hol)
    lag = stats.lag_sessions(last, want, hol)
    if lag is None:
        return Check(ds.name, "freshness", "unknown", note="never loaded", critical=ds.critical), want
    note = f"last loaded {last}, expected {want}" if lag else None
    return Check(ds.name, "freshness", "fail" if lag else "ok", lag, 0, note, critical=ds.critical), want


def verdict(checks: list[Check]) -> tuple[str, list[str]]:
    failed = [c.check for c in checks if c.critical and c.status == "fail"]
    if failed:
        return "fail", failed
    if any(c.status in ("fail", "warn") for c in checks):
        return "warn", []
    if any(c.critical and c.status == "unknown" for c in checks):
        return "unknown", []
    return "ok", []


def _store(s: Session, day: date, checks: list[Check], now: datetime) -> None:
    for c in checks:
        vals = {"status": c.status, "critical": c.critical, "value": c.value, "threshold": c.threshold,
                "note": c.note, "run_at": now}  # fmt: skip
        st = insert(M.DqCheck).values(dataset=c.dataset, day=day, check=c.check, **vals)
        s.execute(st.on_conflict_do_update(index_elements=["dataset", "day", "check"], set_=vals))


def _status(s: Session, name: str, day: date, last: date | None, lag: int | None, checks: list[Check],
            now: datetime) -> str:  # fmt: skip
    status, failed = verdict(checks)
    vals = {"day": day, "last_loaded": last, "lag_sessions": lag, "status": status, "failed": failed,
            "checked_at": now}  # fmt: skip
    s.execute(insert(M.DatasetStatus).values(dataset=name, **vals)
              .on_conflict_do_update(index_elements=["dataset"], set_=vals))  # fmt: skip
    return status


def _guarded(
    s: Session, name: str, critical: bool, fn: Callable[..., list[Check]], *args: Any
) -> list[Check]:
    """One dataset's checks; a crash becomes an unknown `checks` row (with the error) instead of stopping the pass."""
    try:
        with s.begin_nested():
            return fn(*args)
    except Exception as e:
        log.warning("data-quality checks for %s failed", name, exc_info=True)
        return [Check(name, "checks", "unknown", note=f"{type(e).__name__}: {e}", critical=critical)]


def run_all(s: Session, now: datetime, *, holidays: set[date] | None = None,
            free_bytes: Callable[[Any], int] | None = None) -> dict[str, Any]:  # fmt: skip
    """Run every dataset's checks as of `now` and store the results. Returns {checks, status: {dataset: status}}."""
    from finresearch.config import get_settings

    today = to_ist(now).date()
    hol = _holidays(today, holidays)
    out: dict[str, str] = {}
    n = 0
    for ds in DATASETS:
        last = ds.last(s)
        fresh, want = freshness(ds, last, now, hol)
        day = want if want is not None and last is not None and last >= want else last
        checks = [fresh] + (_guarded(s, ds.name, ds.critical, ds.checks, s, day) if day is not None else [])
        key = want or day or today
        _store(s, key, [fresh], now)
        if day is not None:
            _store(s, day, checks[1:], now)
        out[ds.name] = _status(s, ds.name, key, last, fresh.value if fresh.status != "unknown" else None,
                               checks, now)  # fmt: skip
        n += len(checks)
    session = stats.expected_day(now, time(16, 0), hol or set())
    free = {"free_bytes": free_bytes} if free_bytes else {}
    extra = [*_guarded(s, "ipo_history", False, misc.ipo_checks, s, now),
             *_guarded(s, "groww_close", False, misc.close_checks, s, session),
             misc.disk_check(get_settings().state_dir.parent, **free)]  # fmt: skip
    for name in sorted({c.dataset for c in extra}):
        mine = [c for c in extra if c.dataset == name]
        _store(s, today, mine, now)
        out[name] = _status(s, name, today, None, None, mine, now)
        n += len(mine)
    return {"checks": n, "status": out}

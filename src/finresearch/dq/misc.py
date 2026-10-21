"""Checks on the forward quote archive, the intraday archive, the IPO tables, Groww's closes and the disk (#368).

* quotes: one snapshot per registered time (fnohist.quotes.SNAPSHOT_TIMES) on a session, and the share of quotes
  flagged STALE (a last trade older than STALE_AFTER at the request).
* intraday: every ARCHIVE_INDICES series archived and complete for the session.
* IPO history: 'error' rows whose only reason is a transient network failure (ReadTimeout, DNS) are a failure kept as
  data; counted until they are re-harvested. The issue registry is refreshed twice a day, so a newest row older than
  REGISTRY_MAX_AGE means the refresh has stopped.
* Groww vs official close (reconciliation d, groww_close_check): mismatches on the day. Public prices, no holdings.
* disk: below MIN_FREE_GIB free on the data volume, ingests stop (`ingest_allowed`) and the check fails.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import GrowwCloseCheck, IntradaySeriesRow, IpoHistory, IpoIssue, OptionQuote
from finresearch.dq.base import Check, share_status
from finresearch.fincalc.dates import IST

STALE_SHARE = 0.5
REGISTRY_MAX_AGE = timedelta(days=2)
MIN_FREE_GIB = 10
TRANSIENT = re.compile(r"ReadTimeout|ConnectTimeout|ConnectError|PoolTimeout|RemoteProtocolError|TimeoutException|"
                       r"nodename nor servname|Name or service not known|Temporary failure in name resolution")  # fmt: skip


def _ist_day(day: date) -> tuple[datetime, datetime]:
    a = datetime.combine(day, datetime.min.time(), IST)
    return a, a + timedelta(days=1)


def quote_checks(s: Session, day: date) -> list[Check]:
    from finresearch.fnohist import quotes as fq

    a, b = _ist_day(day)
    n_snap = s.scalar(select(func.count(func.distinct(OptionQuote.snapshot_at)))
                      .where(OptionQuote.snapshot_at >= a, OptionQuote.snapshot_at < b)) or 0  # fmt: skip
    want = len(fq.SNAPSHOT_TIMES)
    out = [Check("option_quote", "snapshots", "ok" if n_snap >= want else "fail", n_snap, want,
                 None if n_snap >= want else f"{n_snap} of {want} snapshots on {day}", critical=False)]  # fmt: skip
    n, stale = s.execute(select(func.count(), func.count().filter(OptionQuote.flags.op("&")(fq.STALE) != 0))
                         .where(OptionQuote.snapshot_at >= a, OptionQuote.snapshot_at < b)).one()  # fmt: skip
    st, v = share_status(stale, n, STALE_SHARE)
    out.append(Check("option_quote", "stale_share", "warn" if st == "fail" else st, v, STALE_SHARE,
                     f"{stale}/{n} quotes stale", critical=False))  # fmt: skip
    return out


def intraday_checks(s: Session, day: date) -> list[Check]:
    from finresearch.monitor.intraday import ARCHIVE_INDICES

    done = set(s.scalars(select(IntradaySeriesRow.symbol).where(
        IntradaySeriesRow.kind == "index", IntradaySeriesRow.day == day, IntradaySeriesRow.complete.is_(True))))  # fmt: skip
    miss = [x for x in ARCHIVE_INDICES if x not in done]
    return [Check("intraday_series", "indices_complete", "fail" if miss else "ok", len(miss), 0,
                  f"not archived: {', '.join(miss)}" if miss else None, critical=False)]  # fmt: skip


def ipo_checks(s: Session, now: datetime) -> list[Check]:
    errs = s.scalars(select(IpoHistory.missing).where(IpoHistory.status == "error")).all()
    n = sum(1 for m in errs if m and all(TRANSIENT.search(str(x)) for x in m))
    out = [Check("ipo_history", "transient_errors_final", "warn" if n else "ok", n, 0,
                 f"{n} row(s) failed on a network error and are kept as final 'error' rows" if n else None,
                 critical=False)]  # fmt: skip
    last = s.scalar(select(func.max(IpoIssue.last_seen)))
    if last is None:
        out.append(Check("ipo_issue", "registry_fresh", "unknown", note="the issue registry is empty"))
    else:
        age = now - last
        out.append(Check("ipo_issue", "registry_fresh", "fail" if age > REGISTRY_MAX_AGE else "ok",
                         round(age.total_seconds() / 3600, 1), REGISTRY_MAX_AGE.total_seconds() / 3600,
                         f"last refreshed {last:%d %b %H:%M} UTC"))  # fmt: skip
    return out


def close_checks(s: Session, day: date) -> list[Check]:
    n, bad = s.execute(select(func.count(), func.count().filter(GrowwCloseCheck.matched.is_(False)))
                       .where(GrowwCloseCheck.day == day)).one()  # fmt: skip
    if not n:
        return [
            Check(
                "groww_close",
                "groww_vs_official",
                "unknown",
                note=f"no close checks for {day}",
                critical=False,
            )
        ]
    return [Check("groww_close", "groww_vs_official", "warn" if bad else "ok", bad, 0,
                  f"{bad}/{n} Groww closes differ from the official close" if bad else None, critical=False)]  # fmt: skip


def _free(p: Path) -> int:
    return shutil.disk_usage(p).free


def disk_check(path: Path, free_bytes: Callable[[Path], int] = _free) -> Check:
    try:
        gib = free_bytes(path) / 2**30
    except OSError as e:
        return Check("disk", "free_space", "unknown", note=f"cannot read free space: {type(e).__name__}")
    return Check("disk", "free_space", "fail" if gib < MIN_FREE_GIB else "ok", round(gib, 1), MIN_FREE_GIB,
                 f"{gib:.1f} GiB free on the data volume")  # fmt: skip


def ingest_allowed(path: Path, free_bytes: Callable[[Path], int] = _free) -> bool:
    return disk_check(path, free_bytes).status == "ok"

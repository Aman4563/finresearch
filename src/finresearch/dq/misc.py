"""Checks on the forward quote archive, the intraday archive, the IPO tables, Groww's closes and the disk (#368).

* quotes: one snapshot per registered time (fnohist.quotes.SNAPSHOT_TIMES) on a session, and the share of quotes
  flagged STALE (a last trade older than STALE_AFTER at the request).
* intraday: every ARCHIVE_INDICES series archived and complete for the session.
* IPO history: 'error' rows whose only reason is a transient network failure (ReadTimeout, DNS) are re-fetched by the
  nightly harvest with a backoff (evals.ipo_history.retry_due, #433); those that used up their attempts are a failure
  kept as data and counted until they are re-harvested. The issue registry is refreshed twice a day, so a newest row
  older than REGISTRY_MAX_AGE means the refresh has stopped.
* Groww vs official close (reconciliation d, groww_close_check): closes beyond CLOSE_WARN_TOL on the day (Groww's
  last trade vs the exchange's 30-minute weighted average differ by paise on most stocks). Public prices, no holdings.
* disk: below MIN_FREE_GIB free on the data volume, ingests stop (`ingest_allowed`) and the check fails.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import GrowwCloseCheck, IntradaySeriesRow, IpoHistory, IpoIssue, OptionQuote
from finresearch.dq.base import Check, share_status
from finresearch.fincalc.dates import IST

STALE_SHARE = 0.5
REGISTRY_MAX_AGE = timedelta(days=2)
MIN_FREE_GIB = 10
# Groww's close is the last traded price; NSE's official closing price is the volume-weighted average price of the
# trades in the last 30 minutes of the continuous session, or the last traded price when there was no trade in that
# window (NSE's closing-price method as brokers' explainers quote it, e.g. Kotak Securities' "Why is there a difference
# between last traded price and closing price on NSE?"; BSE uses the same 30-minute average). F&O stocks moved to a
# closing auction session in Aug-2026 per broker notes (not confirmed from an NSE circular here); its price also differs
# from the last trade. Live groww_close_check rows (Oct-2026): median |diff| 0.1 %, max 1.2 %. Beyond 2 % the gap is
# more than the method explains (a wrong day, instrument or an unadjusted corporate action), so only that warns.
CLOSE_WARN_TOL = Decimal("0.02")


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
    from finresearch.evals.ipo_history import MAX_FETCH_ATTEMPTS, fetch_attempts, transient_only

    errs = s.execute(select(IpoHistory.missing, IpoHistory.source).where(IpoHistory.status == "error")).all()
    net = [src for m, src in errs if transient_only(m)]
    n = sum(1 for src in net if fetch_attempts(src) >= MAX_FETCH_ATTEMPTS)  # retries used up: final
    waiting = len(net) - n
    parts = (
        [
            f"{n} row(s) failed on a network error {MAX_FETCH_ATTEMPTS} times and are kept as final 'error' rows"
        ]
        if n
        else []
    )
    if waiting:
        parts.append(f"{waiting} awaiting a retry by the nightly harvest")
    out = [Check("ipo_history", "transient_errors_final", "warn" if n else "ok", n, 0, "; ".join(parts) or None,
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
    """Groww's close vs the official close on `day`: warn only beyond CLOSE_WARN_TOL (see the constant). `matched`
    (₹0.01, groww_market.CLOSE_MATCH_TOL) is counted for the note only; a row with no official close is unknown."""
    C = GrowwCloseCheck
    rel = func.abs(C.groww_close - C.official_close) / func.nullif(C.official_close, 0)
    n, unmatched, beyond, no_official, worst = s.execute(
        select(func.count(), func.count().filter(C.matched.is_(False)), func.count().filter(rel > CLOSE_WARN_TOL),
               func.count().filter(rel.is_(None)), func.max(rel)).where(C.day == day)).one()  # fmt: skip
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
    tol = f"{CLOSE_WARN_TOL * 100:.0f} %"
    parts = [f"{beyond}/{n} Groww closes differ from the official close by more than {tol}" if beyond else
             f"{unmatched} of {n} differ by more than ₹0.01 (last trade vs 30-minute average), all within {tol}"]  # fmt: skip
    if worst is not None:
        parts.append(f"max {worst * 100:.2f} %")
    if no_official:
        parts.append(f"{no_official} without an official close")
    status = "warn" if beyond else "unknown" if no_official else "ok"
    return [Check("groww_close", "groww_vs_official", status, beyond, float(CLOSE_WARN_TOL), "; ".join(parts),
                  critical=False)]  # fmt: skip


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

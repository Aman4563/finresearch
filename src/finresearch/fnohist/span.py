"""Store NSE's daily SPAN risk parameters (#370): the nightly job (monitor.span) and `finresearch fno span backfill`.

What is kept (the issue's disk budget): per underlying per day only the price, priceScan, volScan, the short-option
minimum (somRate) and the calendar spread tiers (dSpread), in `span_param` (~250 rows, a few KB a day), plus NSE's
FOVOLT volatilities in `fo_volt`. The raw zip (~10 MB; the full risk arrays) is kept under data/span/<year>/ for the
last KEEP_SESSIONS sessions only, for the margin engine (P4.3, `arrays`), and older zips are deleted after each
nightly load. The XML (~52 MB a day) is never written to disk: it is stream-parsed from the zip.

Backfill: one file a month (MF8: ~220 files from 2008 instead of ~4,500), the first session on or after the 15th
(SAMPLE_DAY): the 15th is tried first, then the following days until a file is found (a weekend, a holiday or a
missing day answers 404, recorded as no_file and not asked again). A month with any loaded day is done. Backfilled
zips are parsed and discarded (keep_raw off). `--daily` fetches every session instead (a disk and request decision).

Politeness (owner decision #362, finresearch.data_rights): the shared NSE budget, `pause` seconds between downloads,
no downloads between 09:00 and 16:00 IST on weekdays, and a stop after two refusals (403/429) in a row or five
failures of any kind; re-run the same command to resume.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.adapters import nse_span as sp
from finresearch.db.models import FoVolt, SpanDay, SpanParam
from finresearch.refseries.index_eod import in_market_hours, refused

log = logging.getLogger(__name__)
DATASET = "nse_span"
KEEP_SESSIONS = 30
SAMPLE_DAY = 15
SAMPLE_TRIES = 10  # calendar days from the 15th before a month is left for the next run
RECHECK_DAYS = 3
MAX_CONSECUTIVE_FAILURES = 5
REFUSALS_TO_STOP = 2
MAX_AGE_DAYS = 7  # params_for: an older file is not "today's ranges"


def raw_dir() -> Path:
    from finresearch.config import get_settings

    return Path(get_settings().state_dir).parent / "span"


def raw_path(day: date) -> Path:
    return raw_dir() / str(day.year) / sp.url_for(day).rsplit("/", 1)[1]


def _manifest(s: Session, day: date, **values: Any) -> None:
    stmt = pg_insert(SpanDay).values(day=day, attempts=1, **values)
    s.execute(stmt.on_conflict_do_update(index_elements=["day"], set_={
        **values, "attempts": SpanDay.attempts + 1, "fetched_at": stmt.excluded.fetched_at}))  # fmt: skip


def mark(s: Session, day: date, status: str, error: str | None = None) -> None:
    _manifest(s, day, status=status, error=None if error is None else error[:300])


def load_day(s: Session, day: date, url: str, content: bytes) -> int:
    """Replace `day`'s parameters with the file's (in the caller's transaction). Raises NseError for a file that is
    not a SPAN file or is dated another day."""
    f = sp.parse(content)
    if f.day != day:
        raise sp.NseError(f"the SPAN file for {day} carries another day ({f.day})")
    s.execute(delete(SpanParam).where(SpanParam.day == day))
    rows = [{"day": day, "symbol": p.symbol, "price": p.price, "price_scan": p.price_scan, "vol_scan": p.vol_scan,
             "som_rate": p.som_rate, "spreads": p.spreads} for p in f.params.values()]  # fmt: skip
    if rows:
        s.execute(insert(SpanParam), rows)
    _manifest(
        s, day, status="loaded", url=url, rows=len(rows), created=f.created, points=f.points, error=None
    )
    return len(rows)


def load_volt(s: Session, day: date, text: str) -> int:
    rows = sp.parse_volt(text, day)
    s.execute(delete(FoVolt).where(FoVolt.day == day))
    if rows:
        cols = [c for c in sp.VoltRow.__slots__]  # type: ignore[attr-defined]
        s.execute(insert(FoVolt), [{c: getattr(r, c) for c in cols} for r in rows])
    return len(rows)


def _manifest_rows(s: Session, start: date, end: date) -> dict[date, SpanDay]:
    return {r.day: r for r in s.scalars(select(SpanDay).where(SpanDay.day.between(start, end)))}


def _skip(d: date, m: SpanDay | None, today: date, hol: set[date] | None, bhav: dict[date, str]) -> bool:
    """A day not worth a request: loaded, an old 404, a weekend with no F&O session on record, a known holiday."""
    stale = today - timedelta(days=RECHECK_DAYS)
    if m is not None and (m.status == "loaded" or (m.status == "no_file" and d < stale)):
        return True
    if d.weekday() >= 5:
        return bhav.get(d) != "loaded"
    return m is None and bool((hol and d in hol) or (bhav.get(d) == "no_file" and d < stale))


def plan_days(s: Session, start: date, end: date, today: date, holidays: set[date] | None = None,
              monthly: bool = True) -> list[date]:  # fmt: skip
    """The days a run still has to try, oldest first. Monthly: per month without a loaded day, the candidates from
    the SAMPLE_DAY on (the run stops trying a month at its first loaded day)."""
    from finresearch.adapters.nse_holidays import known_trading_holidays
    from finresearch.db.models import FnoBhavDay

    end = min(end, today)
    done = _manifest_rows(s, start, end)
    bhav = dict(
        s.execute(select(FnoBhavDay.day, FnoBhavDay.status).where(FnoBhavDay.day.between(start, end))).all()
    )
    years: dict[int, set[date] | None] = {}

    def hol(d: date) -> set[date] | None:
        if holidays is not None:
            return holidays
        if d.year not in years:
            years[d.year] = known_trading_holidays(d)
        return years[d.year]

    out: list[date] = []
    if not monthly:
        d = start
        while d <= end:
            if not _skip(d, done.get(d), today, hol(d), bhav):
                out.append(d)
            d += timedelta(days=1)
        return out
    month = date(start.year, start.month, 1)
    while month <= end:
        nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        if not any(r.status == "loaded" for d, r in done.items() if month <= d < nxt):
            first = date(month.year, month.month, SAMPLE_DAY)
            for i in range(SAMPLE_TRIES):
                d = first + timedelta(days=i)
                if d < start or d > end or d >= nxt:
                    continue
                if not _skip(d, done.get(d), today, hol(d), bhav):
                    out.append(d)
        month = nxt
    return out


def prune_raw(keep: int = KEEP_SESSIONS) -> int:
    """Delete kept zips beyond the newest `keep` (the full risk arrays are kept for the last `keep` sessions only)."""
    files = sorted(raw_dir().glob("*/nsccl.*.s.zip"), key=lambda p: p.name)
    old = files[:-keep] if keep else files
    for p in old:
        p.unlink(missing_ok=True)
    return len(old)


def params_for(s: Session, symbol: str, day: date, max_age_days: int = MAX_AGE_DAYS) -> dict[str, Any] | None:
    """The newest stored scan ranges for `symbol` on or before `day` (within `max_age_days`), as margin_approx
    params with their source; None when no file is stored for that window (the caller then uses its labelled
    assumptions). A range is only returned when both price and priceScan are known."""
    row = s.scalars(select(SpanParam).where(SpanParam.symbol == symbol.upper(), SpanParam.day <= day,
                                            SpanParam.day >= day - timedelta(days=max_age_days))
                    .order_by(SpanParam.day.desc()).limit(1)).first()  # fmt: skip
    if row is None or not row.price or row.price_scan is None:
        return None
    src = f"NSE SPAN file {row.day.isoformat()}"
    params = {"price_scan": float(row.price_scan / row.price)}
    sources = {"price_scan": src}
    if row.vol_scan is not None:
        params["vol_scan"], sources["vol_scan"] = float(row.vol_scan), src
    return {"day": row.day, "params": params, "sources": sources}


def arrays(day: date, symbol: str) -> list[sp.RiskArray] | None:
    """The day's risk arrays for one underlying from the kept zip; None when the zip is not kept (older than
    KEEP_SESSIONS sessions, or a sampled backfill day)."""
    p = raw_path(day)
    return sp.risk_arrays(p.read_bytes(), symbol) if p.is_file() else None


async def _volt(http: Any, day: date) -> int | None:
    """FOVOLT for a loaded day: rows stored, 0 when NSE has no file; raises on a refusal or a bad file."""
    from finresearch.db import session_scope

    got = await sp.fetch_volt(http, day)
    if got is None:
        return 0
    with session_scope() as s:
        return load_volt(s, day, got[1])


async def backfill(start: date, end: date | None = None, *, today: date | None = None, monthly: bool = True,
                   http: Any = None, holidays: set[date] | None = None, pause: float = 0.0, keep_raw: bool = False,
                   volt: bool = True, now: Callable[[], datetime] | None = None,
                   progress: Callable[[int, int, date, str], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Fetch and store the planned days in [start, end] (module docstring). Resumable and idempotent. Returns counts
    by outcome, failed days with their errors, and `stopped` when the run stopped early."""
    from finresearch import data_rights
    from finresearch.adapters.http import PoliteClient, now_ist
    from finresearch.db import session_scope
    from finresearch.fincalc.dates import today_ist

    data_rights.require(DATASET)
    now = now or now_ist
    today = today or today_ist()
    end = min(end or today, today)
    with session_scope() as s:
        days = plan_days(s, start, end, today, holidays, monthly)
    out: dict[str, Any] = {"planned": len(days), "loaded": 0, "no_file": 0, "failed": {}, "rows": 0,
                           "volt_rows": 0, "volt_failed": {}}  # fmt: skip
    own = http is None
    http = http or PoliteClient()
    streak = refusals = 0
    done_months: set[tuple[int, int]] = set()
    try:
        for i, day in enumerate(days, 1):
            if monthly and (day.year, day.month) in done_months:
                continue
            if in_market_hours(now()):
                out["stopped"] = (
                    "market hours (09:00-16:00 IST): no backfill downloads; re-run after the close"
                )
                break
            status = "loaded"
            try:
                got = await sp.fetch_day(http, day)
                if pause:
                    await asyncio.sleep(pause)
                with session_scope() as s:
                    if got is None:
                        mark(s, day, "no_file")
                        status = "no_file"
                    else:
                        out["rows"] += load_day(s, day, *got)
                if got is not None and keep_raw:
                    p = raw_path(day)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(got[1])
                out[status] += 1
                streak = refusals = 0
                if got is not None:
                    done_months.add((day.year, day.month))
                    if volt:
                        try:
                            out["volt_rows"] += await _volt(http, day) or 0
                        except Exception as e:  # FOVOLT is its own file: its failure never un-loads the day
                            out["volt_failed"][day.isoformat()] = f"{type(e).__name__}: {e}"[:300]
            except Exception as e:  # a refusal, a bad file, a transport error: recorded, retried next run
                status = "failed"
                out["failed"][day.isoformat()] = f"{type(e).__name__}: {e}"[:300]
                with session_scope() as s:
                    mark(s, day, "failed", out["failed"][day.isoformat()])
                streak += 1
                refusals = refusals + 1 if refused(e) else 0
                if refusals >= REFUSALS_TO_STOP:
                    out["stopped"] = (f"NSE refused {refusals} requests in a row (403/429): stopped; pause this "
                                      "dataset and check before re-running (#362)")  # fmt: skip
                    break
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    out["stopped"] = (
                        f"{streak} consecutive failures: stopped to stay polite; re-run to resume"
                    )
                    break
            if progress:
                progress(i, len(days), day, status)
    finally:
        if own:
            await http.aclose()
    if keep_raw:
        out["raw_pruned"] = prune_raw()
    return out

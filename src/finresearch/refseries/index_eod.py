"""Store NSE's daily index close file day by day (#366): `finresearch index backfill` and the nightly job
(monitor.index_eod). One GET a day, ~9 KB: every NSE index's OHLC, P/E, P/B, dividend yield, and India VIX.

Resume: `index_eod_day` records every day tried. Loaded days are skipped; a 404 day (status no_file) is skipped once
it is older than RECHECK_DAYS. Requests are kept to likely sessions: weekends are skipped unless the F&O bhavcopy
manifest (fno_bhav_day) loaded a file that day (NSE's special Saturday/Sunday sessions), and a weekday is skipped
when it is a known trading holiday or the bhavcopy manifest found no file for it. Other weekdays are fetched (a
holiday then answers 404, ~3 KB). About 3,350 requests from Mar-2013 to Oct-2026.

Politeness (owner decision #362, finresearch.data_rights): every request goes through the shared NSE budget
(adapters.http); `pause` adds seconds between downloads; the run stops after REFUSALS_TO_STOP refusals (HTTP 403/429)
in a row, or MAX_CONSECUTIVE_FAILURES failures of any kind, and before any download between 09:00 and 16:00 IST on a
weekday (re-run after the close to continue). Raw CSVs are kept under data/index_eod/<year>/ (gitignored, ~30 MB for
2013-2026) unless --no-keep-raw, so a parser change re-reads them without downloading again.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.adapters import nse_index_eod as ie
from finresearch.db.models import IndexEod, IndexEodDay

log = logging.getLogger(__name__)
DATASET = "index_eod"
RECHECK_DAYS = 3
MAX_CONSECUTIVE_FAILURES = 5
REFUSALS_TO_STOP = 2
MARKET_HOURS = (time(9, 0), time(16, 0))  # IST, weekdays: no backfill downloads (owner decision #362)
_REFUSED = re.compile(r"\bHTTP (?:403|429)\b")
COLUMNS = ("day", "symbol", "index_name", "open", "high", "low", "close", "points_change", "pct_change", "volume",
           "turnover_cr", "pe", "pb", "div_yield")  # fmt: skip


def in_market_hours(now: datetime) -> bool:
    from finresearch.fincalc.dates import to_ist

    ist = to_ist(now)
    return ist.weekday() < 5 and MARKET_HOURS[0] <= ist.time() < MARKET_HOURS[1]


def refused(exc: BaseException) -> bool:
    return bool(_REFUSED.search(str(exc)))


def raw_dir() -> Path:
    from finresearch.config import get_settings

    return Path(get_settings().state_dir).parent / "index_eod"


def raw_path(day: date) -> Path:
    return raw_dir() / str(day.year) / ie.url_for(day).rsplit("/", 1)[1]


def _manifest(s: Session, day: date, **values: Any) -> None:
    stmt = pg_insert(IndexEodDay).values(day=day, attempts=1, **values)
    s.execute(stmt.on_conflict_do_update(index_elements=["day"], set_={
        **values, "attempts": IndexEodDay.attempts + 1, "fetched_at": stmt.excluded.fetched_at}))  # fmt: skip


def load_day(s: Session, day: date, url: str, content: bytes) -> int:
    """Replace `day`'s rows with the file's (in the caller's transaction). A symbol printed twice keeps its first
    row. Raises NseError for a file that is not the index close file or is dated another day."""
    rows = ie.parse(content.decode("utf-8", "replace"), day)
    seen: dict[str, ie.IndexRow] = {}
    for r in rows:
        seen.setdefault(r.symbol, r)
    s.execute(delete(IndexEod).where(IndexEod.day == day))
    if seen:
        s.execute(insert(IndexEod), [{c: getattr(r, c) for c in COLUMNS} for r in seen.values()])
    _manifest(s, day, status="loaded", url=url, rows=len(seen), error=None)
    return len(seen)


def mark(s: Session, day: date, status: str, error: str | None = None) -> None:
    _manifest(s, day, status=status, error=None if error is None else error[:300])


def plan_days(
    s: Session, start: date, end: date, today: date, holidays: set[date] | None = None
) -> list[date]:
    """The days a run still has to fetch, oldest first (see the module docstring)."""
    from finresearch.adapters.nse_holidays import known_trading_holidays
    from finresearch.db.models import FnoBhavDay

    done = {r.day: r for r in s.scalars(select(IndexEodDay).where(IndexEodDay.day.between(start, end)))}
    bhav = dict(
        s.execute(select(FnoBhavDay.day, FnoBhavDay.status).where(FnoBhavDay.day.between(start, end))).all()
    )
    stale = today - timedelta(days=RECHECK_DAYS)
    years: dict[int, set[date] | None] = {}
    out, d = [], start
    while d <= min(end, today):
        m = done.get(d)
        if holidays is None and d.year not in years:
            years[d.year] = known_trading_holidays(d)
        hol = holidays if holidays is not None else years[d.year]
        if m is not None and (m.status == "loaded" or (m.status == "no_file" and d < stale)):
            pass
        elif d.weekday() >= 5 and bhav.get(d) != "loaded":
            pass  # a weekend without an F&O session on record
        elif (
            m is None and d.weekday() < 5 and ((hol and d in hol) or (bhav.get(d) == "no_file" and d < stale))
        ):
            pass  # a known weekday trading holiday: no request
        else:
            out.append(d)
        d += timedelta(days=1)
    return out


async def backfill(start: date, end: date | None = None, *, today: date | None = None, http: Any = None,
                   holidays: set[date] | None = None, pause: float = 0.0, keep_raw: bool = True,
                   now: Callable[[], datetime] | None = None,
                   progress: Callable[[int, int, date, str], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Fetch and store every missing day in [start, end]. Resumable and idempotent (module docstring). Returns
    counts by outcome, the failed days with their errors, and `stopped` when the run stopped early."""
    from finresearch import data_rights
    from finresearch.adapters.http import PoliteClient, now_ist
    from finresearch.db import session_scope
    from finresearch.fincalc.dates import today_ist

    data_rights.require(DATASET)
    now = now or now_ist
    today = today or today_ist()
    end = min(end or today, today)
    with session_scope() as s:
        days = plan_days(s, start, end, today, holidays)
    out: dict[str, Any] = {
        "planned": len(days),
        "loaded": 0,
        "no_file": 0,
        "failed": {},
        "rows": 0,
        "from_raw": 0,
    }
    own = http is None
    http = http or PoliteClient()
    streak = refusals = 0
    try:
        for i, day in enumerate(days, 1):
            p = raw_path(day)
            if not p.is_file() and in_market_hours(now()):
                out["stopped"] = (
                    "market hours (09:00-16:00 IST): no backfill downloads; re-run after the close"
                )
                break
            status = "loaded"
            try:
                if p.is_file():
                    got: tuple[str, bytes] | None = (ie.url_for(day), p.read_bytes())
                    out["from_raw"] += 1
                else:
                    got = await ie.fetch_day(http, day)
                    if pause:
                        await asyncio.sleep(pause)
                    if got is not None and keep_raw:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(got[1])
                with session_scope() as s:
                    if got is None:
                        mark(s, day, "no_file")
                        status = "no_file"
                    else:
                        out["rows"] += load_day(s, day, *got)
                out[status] += 1
                streak = refusals = 0
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
    return out

"""Dated rate series from FBIL (#366): `finresearch rates backfill` and the nightly job (monitor.rates) store FBIL's
G-sec par yield curve, T-bill benchmark, overnight MIBOR and term MIBOR in `rate_curve`; refseries.rate_lookup reads
them by date. Nothing here ever fills in a rate FBIL did not publish (no fixed 6.5 %).

Requests (adapters.fbil, through PoliteClient's per-host limit):
* the JSON fixings (tbill, mibor_on, mibor_term): one GET per series per calendar month, ~1-40 KB. History: overnight
  MIBOR from Jul-2015, term MIBOR from Sep-2015, T-bills from 2018. FBIL's public pages lag about a week, so a month
  is asked again until a load finished SETTLE_DAYS after its end; rows are upserted, never deleted.
* the G-sec par curve: one listing GET per year (the days FBIL published a workbook; the first is 31-Mar-2018), then
  one workbook per missing day (~0.73 MB, OLE2 .xls up to 2022 and .xlsx after, adapters.xls). Workbooks are parsed
  and discarded (the curve is ~200 rows). About 2,100 workbooks from 2018, ~1,200 from 2022.

Lineage (dq.lineage): one dataset_load row per day and series when that day's content is new or changed (dataset
`fbil_<series>`): the workbook's sha256 for gsec_par, the sha256 of the day's rows as canonical JSON for the fixings
(FBIL serves months, so a per-day hash is what shows a re-published day).

Politeness: FBIL is not NSE, so #362's market-hours rule does not bind it; the backfill still keeps the same stops so
a long workbook run stays out of FBIL's business day: no new download 09:00-16:00 IST on weekdays (re-run after),
stop after REFUSALS_TO_STOP refusals (HTTP 403/429) in a row or MAX_CONSECUTIVE_FAILURES failures. Data rights:
`data_rights.require("fbil_rates")` first (pending the owner's decision, data_rights.PENDING).
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.adapters import fbil
from finresearch.db.models import DatasetLoad, RateCurve, RateCurveLoad
from finresearch.refseries.index_eod import (
    MAX_CONSECUTIVE_FAILURES,
    REFUSALS_TO_STOP,
    in_market_hours,
    refused,
)

DATASET = "fbil_rates"  # the data-rights entry covering every FBIL series
SERIES = ("gsec_par", "tbill", "mibor_on", "mibor_term")
FIXINGS = ("tbill", "mibor_on", "mibor_term")
FIRST = {"gsec_par": date(2018, 3, 31), "tbill": date(2018, 1, 1), "mibor_on": date(2015, 7, 1),
         "mibor_term": date(2015, 9, 1)}  # fmt: skip  # earliest data seen on FBIL's pages, 22-Oct-2026
SETTLE_DAYS = (
    14  # FBIL's public pages lagged ~1 week on 22-Oct-2026; twice that before a month counts as final
)
PARSER_VERSION = "1"


def lineage_name(series: str) -> str:
    return f"fbil_{series}"


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def months(start: date, end: date) -> list[date]:
    out, d = [], start.replace(day=1)
    while d <= end:
        out.append(d)
        d = _month_end(d) + timedelta(days=1)
    return out


def _manifest(s: Session, series: str, period: date, **values: Any) -> None:
    stmt = pg_insert(RateCurveLoad).values(series=series, period=period, attempts=1, **values)
    s.execute(stmt.on_conflict_do_update(index_elements=["series", "period"], set_={
        **values, "attempts": RateCurveLoad.attempts + 1, "fetched_at": stmt.excluded.fetched_at}))  # fmt: skip


def _lineage(s: Session, series: str, day: date, body: bytes, url: str | None, n: int) -> None:
    """A dataset_load row only when the day's content is new or changed (dq.lineage keeps the prior hash)."""
    from finresearch.dq.lineage import record_load

    name = lineage_name(series)
    last = s.scalars(select(DatasetLoad.sha256).where(DatasetLoad.dataset == name, DatasetLoad.day == day)
                     .order_by(DatasetLoad.id.desc()).limit(1)).first()  # fmt: skip
    if last != hashlib.sha256(body).hexdigest():
        record_load(s, name, day, body, url=url, parser_version=PARSER_VERSION, rows=n)


def _upsert(s: Session, values: list[dict[str, Any]]) -> None:
    if not values:
        return
    stmt = pg_insert(RateCurve).values(values)
    s.execute(stmt.on_conflict_do_update(index_elements=["series", "day", "tenor"], set_={
        c: getattr(stmt.excluded, c) for c in ("tenor_years", "rate", "annualised", "published_at")}))  # fmt: skip


def load_fixings(s: Session, series: str, rows: Iterable[fbil.RateFixing], url: str) -> int:
    """Upsert one window's fixings (the caller's transaction) and record lineage per day. Returns rows stored."""
    by_day: dict[date, list[fbil.RateFixing]] = {}
    for r in rows:
        by_day.setdefault(r.day, []).append(r)
    n = 0
    for day, rs in sorted(by_day.items()):
        vals = [{"series": series, "day": day, "tenor": r.tenor, "tenor_years": Decimal(r.tenor_days) / 365,
                 "rate": r.rate, "annualised": None, "published_at": r.published_at} for r in rs]  # fmt: skip
        _upsert(s, vals)
        canon = json.dumps(sorted([[r.tenor, str(r.rate)] for r in rs]), separators=(",", ":")).encode()
        _lineage(s, series, day, canon, url, len(vals))
        n += len(vals)
    return n


def load_par_curve(s: Session, day: date, content: bytes, url: str) -> int:
    """Parse and store one G-sec workbook; FbilError when it is not `day`'s (a stale or wrong file)."""
    curve = fbil.parse_par_yield(content)
    if curve.as_of != day:
        raise fbil.FbilError(f"workbook for {day} is dated {curve.as_of}")
    _upsert(s, [{"series": "gsec_par", "day": day, "tenor": f"{p.tenor_years.normalize():f}Y",
                 "tenor_years": p.tenor_years, "rate": p.semi_annual, "annualised": p.annualised,
                 "published_at": None} for p in curve.points])  # fmt: skip
    _manifest(s, "gsec_par", day, status="loaded", url=url[:200], rows=len(curve.points), error=None)
    _lineage(s, "gsec_par", day, content, url, len(curve.points))
    return len(curve.points)


class _Stop(Exception):
    pass


def _settled(m: RateCurveLoad | None, period_end: date) -> bool:
    from finresearch.fincalc.dates import to_ist

    return (m is not None and m.status in ("loaded", "no_file")
            and to_ist(m.fetched_at).date() > period_end + timedelta(days=SETTLE_DAYS))  # fmt: skip


async def backfill(series: Iterable[str], start: date, end: date | None = None, *, today: date | None = None,
                   http: Any = None, pause: float = 0.0, now: Callable[[], datetime] | None = None,
                   progress: Callable[[str, str], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Fetch and store the missing periods of each series in [start, end] (each clipped to FIRST[series]).
    Resumable and idempotent (module docstring). Returns counts, the failures and `stopped` when it stopped early."""
    from finresearch import data_rights
    from finresearch.adapters.http import PoliteClient, now_ist
    from finresearch.db import session_scope
    from finresearch.fincalc.dates import today_ist

    names = list(series)
    if bad := [x for x in names if x not in SERIES]:
        raise ValueError(f"unknown rate series {bad} (known: {', '.join(SERIES)})")
    data_rights.require(DATASET)
    now = now or now_ist
    today = today or today_ist()
    end = min(end or today, today)
    out: dict[str, Any] = {"planned": 0, "loaded": 0, "no_file": 0, "failed": {}, "rows": 0}
    st = {"streak": 0, "refusals": 0}
    own = http is None
    client = fbil.FbilClient(http or PoliteClient())

    def gate() -> None:
        if in_market_hours(now()):
            out["stopped"] = "09:00-16:00 IST on a weekday: no backfill downloads; re-run after the close"
            raise _Stop

    def failed(key: str, e: BaseException, period: tuple[str, date] | None) -> None:
        out["failed"][key] = f"{type(e).__name__}: {e}"[:300]
        if period is not None:
            with session_scope() as s:
                _manifest(s, *period, status="failed", url=None, rows=None, error=out["failed"][key])
        st["streak"] += 1
        st["refusals"] = st["refusals"] + 1 if refused(e) else 0
        if st["refusals"] >= REFUSALS_TO_STOP:
            out["stopped"] = (
                f"FBIL refused {st['refusals']} requests in a row (403/429): stopped; check before re-running"
            )
            raise _Stop
        if st["streak"] >= MAX_CONSECUTIVE_FAILURES:
            out["stopped"] = f"{st['streak']} consecutive failures: stopped to stay polite; re-run to resume"
            raise _Stop

    def done(key: str, status: str) -> None:
        st["streak"] = st["refusals"] = 0
        out[status] += 1
        if progress:
            progress(key, status)

    async def fixings(name: str, lo: date, hi: date) -> None:
        with session_scope() as s:
            seen = {m.period: m for m in s.scalars(select(RateCurveLoad).where(RateCurveLoad.series == name))}
        todo = [m for m in months(lo, hi) if not _settled(seen.get(m), _month_end(m))]
        out["planned"] += len(todo)
        for m in todo:
            gate()
            # always the whole month from its 1st: a partial window would mark the month settled half-fetched
            a, b, key = m, min(_month_end(m), hi), f"{name} {m:%Y-%m}"
            try:
                url, _, got = await client.fixings(name, a, b)
                with session_scope() as s:
                    n = load_fixings(s, name, got, url)
                    _manifest(
                        s, name, m, status="loaded" if n else "no_file", url=url[:200], rows=n, error=None
                    )
                out["rows"] += n
                done(key, "loaded" if n else "no_file")
            except _Stop:
                raise
            except (
                Exception
            ) as e:  # a refusal, a layout change, a transport error: recorded, retried next run
                failed(key, e, (name, m))
            if pause:
                await asyncio.sleep(pause)

    async def gsec(lo: date, hi: date) -> None:
        for year in range(lo.year, hi.year + 1):
            gate()
            a, b = max(lo, date(year, 1, 1)), min(hi, date(year, 12, 31))
            try:
                days = await client.gsec_dates(a, b)
            except Exception as e:
                failed(f"gsec_par list {year}", e, None)
                continue
            with session_scope() as s:
                have = set(s.scalars(select(RateCurveLoad.period).where(
                    RateCurveLoad.series == "gsec_par", RateCurveLoad.status == "loaded",
                    RateCurveLoad.period.between(a, b))))  # fmt: skip
            todo = [d for d in days if a <= d <= b and d not in have]
            out["planned"] += len(todo)
            for d in todo:
                gate()
                key = f"gsec_par {d.isoformat()}"
                try:
                    curve_url = f"{fbil.DOWNLOAD_URL}?date={d.isoformat()}"
                    content = await client.workbook(d)
                    with session_scope() as s:
                        out["rows"] += load_par_curve(s, d, content, curve_url)
                    done(key, "loaded")
                except Exception as e:
                    failed(key, e, ("gsec_par", d))
                if pause:
                    await asyncio.sleep(pause)

    try:
        for name in names:
            lo, hi = max(start, FIRST[name]), end
            if lo > hi:
                continue
            await (gsec(lo, hi) if name == "gsec_par" else fixings(name, lo, hi))
    except _Stop:
        pass
    finally:
        if own:
            await client.http.aclose()
    return out

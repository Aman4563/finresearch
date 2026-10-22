"""Store NSE's daily full equity bhavcopy day by day (#373 (a)): `finresearch equity backfill` and the nightly job
(monitor.equity_eod). One GET a day, ~400 KB: every cash-market security's OHLC, official close and delivery. The
nightly stock peer build reads its closes instead of one quote and one history request per stock when
`Settings.peer_price_source` is "equity_eod" (signals.stock_peers).

Resume and request planning are the index close file's (refseries.index_eod.plan_days over `equity_eod_day`): loaded
days are skipped, a 404 day is re-tried for RECHECK_DAYS, weekends are fetched only when the F&O bhavcopy manifest has
a session that day, and known weekday trading holidays are not requested. Each file loaded gets a lineage row
(dq.lineage, dataset "equity_eod"), so a re-uploaded file is seen.

Politeness (owner decision #362, finresearch.data_rights): every request goes through the shared NSE budget
(adapters.http); `pause` adds seconds between downloads; the run stops after REFUSALS_TO_STOP refusals (HTTP 403/429)
in a row, or MAX_CONSECUTIVE_FAILURES failures of any kind, and before any download between 09:00 and 16:00 IST on a
weekday (re-run after the close to continue). Raw CSVs are kept under data/equity_eod/<year>/ (gitignored, ~100 MB a
year) unless --no-keep-raw, so a parser change re-reads them without downloading again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.adapters import nse_equity_eod as ee
from finresearch.db.models import EquityEod, EquityEodDay
from finresearch.refseries.index_eod import (
    MAX_CONSECUTIVE_FAILURES,
    REFUSALS_TO_STOP,
    in_market_hours,
    refused,
)

log = logging.getLogger(__name__)
DATASET = "equity_eod"
# the series whose close is a stock's price, in order: rolling settlement, then trade-to-trade (BE, BZ); SME
# boards last. Bonds, G-secs and other instruments listed under the same symbol (N1, GS, ...) are never a price.
PRICE_SERIES = ("EQ", "BE", "BZ", "SM", "ST")
COLUMNS = ("day", "symbol", "series", "prev_close", "open", "high", "low", "last", "close", "avg_price", "qty",
           "turnover_lacs", "trades", "deliv_qty", "deliv_pct")  # fmt: skip


def raw_dir() -> Path:
    from finresearch.config import get_settings

    return Path(get_settings().state_dir).parent / "equity_eod"


def raw_path(day: date) -> Path:
    return raw_dir() / str(day.year) / ee.url_for(day).rsplit("/", 1)[1]


def _manifest(s: Session, day: date, **values: Any) -> None:
    stmt = pg_insert(EquityEodDay).values(day=day, attempts=1, **values)
    s.execute(stmt.on_conflict_do_update(index_elements=["day"], set_={
        **values, "attempts": EquityEodDay.attempts + 1, "fetched_at": stmt.excluded.fetched_at}))  # fmt: skip


def load_day(s: Session, day: date, url: str, content: bytes) -> int:
    """Replace `day`'s rows with the file's (in the caller's transaction) and record the file's lineage. A
    (symbol, series) printed twice keeps its first row. Raises NseError for a file that is not the full bhavcopy or
    is dated another day."""
    from finresearch.dq.lineage import record_load

    seen: dict[tuple[str, str], ee.EquityRow] = {}
    for r in ee.parse(content.decode("utf-8", "replace"), day):
        seen.setdefault((r.symbol, r.series), r)
    s.execute(delete(EquityEod).where(EquityEod.day == day))
    if seen:
        s.execute(insert(EquityEod), [{c: getattr(r, c) for c in COLUMNS} for r in seen.values()])
    _manifest(s, day, status="loaded", url=url, rows=len(seen), error=None)
    record_load(s, DATASET, day, content, url=url, parser_version=ee.PARSER_VERSION, rows=len(seen))
    return len(seen)


def mark(s: Session, day: date, status: str, error: str | None = None) -> None:
    _manifest(s, day, status=status, error=None if error is None else error[:300])


def plan_days(s: Session, start: date, end: date, today: date,
              holidays: set[date] | None = None) -> list[date]:  # fmt: skip
    from finresearch.refseries.index_eod import plan_days as plan

    return plan(s, start, end, today, holidays, manifest=EquityEodDay)


def is_loaded(s: Session, day: date) -> bool:
    m = s.get(EquityEodDay, day)
    return m is not None and m.status == "loaded"


def _pick(rows: list[tuple[str, Decimal | None]]) -> tuple[Decimal, str] | None:
    by = {series: close for series, close in rows if close is not None}
    return next(((by[x], x) for x in PRICE_SERIES if x in by), None)


def close_on(s: Session, symbol: str, day: date) -> tuple[Decimal, str] | None:
    """(official close, series) of `symbol` on `day` from the first PRICE_SERIES it is listed in; None when the day
    is not stored or the stock has no price row (unknown, never 0)."""
    rows = s.execute(select(EquityEod.series, EquityEod.close).where(EquityEod.day == day, EquityEod.symbol == symbol,
                                                                     EquityEod.series.in_(PRICE_SERIES))).all()  # fmt: skip
    return _pick([tuple(r) for r in rows])


def closes_between(s: Session, symbol: str, start: date, end: date) -> list[tuple[date, Decimal]]:
    """`symbol`'s stored closes in [start, end], oldest first, one per day (the close_on series rule)."""
    rows = s.execute(select(EquityEod.day, EquityEod.series, EquityEod.close)
                     .where(EquityEod.symbol == symbol, EquityEod.day.between(start, end),
                            EquityEod.series.in_(PRICE_SERIES))).all()  # fmt: skip
    per: dict[date, list[tuple[str, Decimal | None]]] = {}
    for d, series, close in rows:
        per.setdefault(d, []).append((series, close))
    out = []
    for d in sorted(per):
        got = _pick(per[d])
        if got is not None:
            out.append((d, got[0]))
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
                    got: tuple[str, bytes] | None = (ee.url_for(day), p.read_bytes())
                    out["from_raw"] += 1
                else:
                    http = http or PoliteClient()
                    got = await ee.fetch_day(http, day)
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
        if own and http is not None:
            await http.aclose()
    return out


async def reconcile_history(n: int = 50, *, seed: int = 0, fetch: Callable[[str, date], Any] | None = None,
                            now: Callable[[], datetime] | None = None, pause: float = 0.0) -> dict[str, Any]:  # fmt: skip
    """#373 (a)'s acceptance check: `n` stored EQ stock-days, drawn reproducibly for `seed`, against NSE's
    price-history bar (`chClosingPrice`, adapters.nse_equity.NseEquity.history), exact. One website request per
    stock-day through the shared NSE budget; never starts between 09:00 and 16:00 IST on a weekday and stops after
    REFUSALS_TO_STOP refusals in a row. A bar NSE does not return is listed as missing, never as a match.
    `fetch(symbol, day)` -> the history close (tests: a fake)."""
    from finresearch.adapters.http import now_ist
    from finresearch.db import session_scope

    now = now or now_ist
    out: dict[str, Any] = {
        "sampled": 0,
        "compared": 0,
        "matches": 0,
        "mismatches": [],
        "missing": [],
        "errors": {},
    }
    if in_market_hours(now()):
        out["stopped"] = "market hours (09:00-16:00 IST): no website requests; re-run after the close"
        return out
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT e.day, e.symbol, e.close FROM equity_eod e JOIN equity_eod_day m ON m.day = e.day
             WHERE m.status = 'loaded' AND e.series = 'EQ' AND e.close IS NOT NULL
             ORDER BY md5(e.symbol || e.day::text || :seed) LIMIT :n"""), {"seed": str(seed), "n": n}).all()  # fmt: skip
    out["sampled"] = len(rows)
    eq = None
    if fetch is None:
        from finresearch.adapters.nse_equity import NseEquity

        eq = NseEquity()

        async def fetch(symbol: str, day: date) -> Decimal | None:
            bars = await eq.history(symbol, day, day)
            return next((b.close for b in bars if b.day == day), None)

    refusals = 0
    try:
        for day, symbol, close in rows:
            try:
                got = await fetch(symbol, day)
                refusals = 0
            except Exception as e:  # recorded; a refusal streak stops the run
                out["errors"][f"{symbol} {day}"] = f"{type(e).__name__}: {e}"[:200]
                refusals = refusals + 1 if refused(e) else 0
                if refusals >= REFUSALS_TO_STOP:
                    out["stopped"] = f"NSE refused {refusals} requests in a row (403/429): stopped (#362)"
                    break
                continue
            finally:
                if pause:
                    await asyncio.sleep(pause)
            if got is None:
                out["missing"].append({"symbol": symbol, "day": day.isoformat()})
                continue
            out["compared"] += 1
            if Decimal(got) == close:
                out["matches"] += 1
            else:
                out["mismatches"].append({"symbol": symbol, "day": day.isoformat(), "bhavcopy": str(close),
                                          "history": str(got)})  # fmt: skip
    finally:
        if eq is not None:
            await eq.nse.aclose()
    return out

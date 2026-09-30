"""Archive of each session's 1-minute price series, so multi-day intraday charts (5D) work.

NSE's and BSE's quote pages serve only the current session's 1-minute series (adapters.nse_intraday,
adapters.bse_intraday; BSE rows are keyed "BSE:<scrip code>"); nothing older is published at 1-minute resolution. Two writers keep `intraday_series`:
- the intraday API stores every series it fetches (at most every few minutes per symbol), so a symbol you look at
  archives itself;
- after the close (from 15:45 IST on trading days) the monitor fetches the full session once for watched stocks, the
  main indices and every symbol viewed today whose row is not complete yet (at most 3 tries, 10 minutes apart).

NSE's terms of use prohibit systematic data collection from its website; this keeps the footprint to one request per
symbol per day for a personal watch list (see the timeframes research notes).
"""

from __future__ import annotations

import logging
import time as _time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from finresearch.adapters.http import IST
from finresearch.adapters.nse_intraday import IntradaySeries
from finresearch.db import session_scope
from finresearch.db.models import IntradaySeriesRow, Watch
from finresearch.fincalc.candles import Tick
from finresearch.fincalc.dates import is_business_day, to_ist

log = logging.getLogger(__name__)
START = (15, 45)  # IST: the close is 15:30; the series settles a few minutes later
COMPLETE_AFTER = time(15, 35)
ARCHIVE_INDICES = ("NIFTY 50", "NIFTY BANK", "NIFTY NEXT 50")
MAX_TRIES, RETRY_S = 3, 600
MAX_SYMBOLS = 40
_tries: dict[tuple[str, str, date], tuple[int, float]] = {}

Fetch = Callable[[str, str], Awaitable[IntradaySeries]]  # (kind, symbol) -> series


def to_json(ticks: list[Tick]) -> list[list[Any]]:
    """[[NSE-style ms (IST wall clock as epoch), price, phase(, volume)]] — the NSE payload's own encoding; the volume
    is kept when the source has one (BSE)."""
    out = []
    for t in ticks:
        wall = t.at.astimezone(IST).replace(tzinfo=None)
        ms = int((wall - datetime(1970, 1, 1)).total_seconds() * 1000)
        out.append([ms, str(t.price), t.phase] + ([str(t.volume)] if t.volume is not None else []))
    return out


def from_json(rows: list[list[Any]]) -> list[Tick]:
    from finresearch.adapters.nse_intraday import decode_ts

    return [Tick(decode_ts(r[0]), Decimal(str(r[1])), r[2] if len(r) > 2 else "NM",
                 Decimal(str(r[3])) if len(r) > 3 and r[3] is not None else None) for r in rows or []]  # fmt: skip


def is_complete(day: date, now: datetime) -> bool:
    local = to_ist(now)
    return local.date() > day or local.time() >= COMPLETE_AFTER


def store(session, series: IntradaySeries, now: datetime) -> bool:
    """Upsert the series' session (its day is its newest sample's date). A complete row is never replaced by an
    incomplete one, and a row never shrinks. Returns True when something was written."""
    if not series.ticks:
        return False
    day, complete = series.day, is_complete(series.day, now)
    row = session.scalar(select(IntradaySeriesRow).where(IntradaySeriesRow.kind == series.kind,
                                                         IntradaySeriesRow.symbol == series.symbol,
                                                         IntradaySeriesRow.day == day))  # fmt: skip
    if row is not None and ((row.complete and not complete) or row.tick_count > len(series.ticks)):
        return False
    values = {"prev_close": series.prev_close, "ticks": to_json(series.ticks), "tick_count": len(series.ticks),
              "last_at": series.as_of, "complete": complete, "source": series.source[:200], "updated_at": now}  # fmt: skip
    stmt = insert(IntradaySeriesRow).values(kind=series.kind, symbol=series.symbol, day=day, **values)
    session.execute(stmt.on_conflict_do_update(index_elements=["kind", "symbol", "day"], set_=values))
    return True


def load_days(session, kind: str, symbol: str, n: int, before: date | None = None) -> list[IntradaySeriesRow]:
    """The newest `n` archived sessions (optionally strictly before `before`), oldest first."""
    q = select(IntradaySeriesRow).where(IntradaySeriesRow.kind == kind, IntradaySeriesRow.symbol == symbol)
    if before is not None:
        q = q.where(IntradaySeriesRow.day < before)
    rows = session.scalars(q.order_by(IntradaySeriesRow.day.desc()).limit(n)).all()
    return list(reversed(rows))


def targets(today: date) -> list[tuple[str, str]]:
    """(kind, symbol) to complete today: main indices, active stock watches, and anything viewed today."""
    with session_scope() as s:
        watched = s.scalars(
            select(Watch.nse_symbol).where(Watch.active.is_(True), Watch.kind == "stock")
        ).all()
        rows = s.execute(select(IntradaySeriesRow.kind, IntradaySeriesRow.symbol, IntradaySeriesRow.complete)
                         .where(IntradaySeriesRow.day == today)).all()  # fmt: skip
    done = {(k, sym) for k, sym, c in rows if c}
    want = [("index", x) for x in ARCHIVE_INDICES]
    want += [("equity", w.upper()) for w in sorted(set(watched)) if w and not w.startswith("BSE:")]
    want += [(k, sym) for k, sym, c in rows if not c]
    out: list[tuple[str, str]] = []
    for t in want:
        if t not in done and t not in out:
            out.append(t)
    return out[:MAX_SYMBOLS]


async def archive_after_close(
    fetch: Fetch, now: datetime, holidays: dict[date, str] | None = None
) -> dict[str, Any]:
    """Complete today's rows after the close. Makes no network call once everything is stored or out of tries."""
    local = to_ist(now)
    today = local.date()
    if (local.hour, local.minute) < START:
        return {"archived": [], "failed": {}, "skipped": "before the after-close window"}
    if holidays is None:
        from finresearch.adapters.nse_holidays import load_holidays

        try:
            holidays = load_holidays("trading")
        except Exception:
            holidays = {}
    if not is_business_day(today, holidays):
        return {"archived": [], "failed": {}, "skipped": "not a trading day"}
    try:
        todo = targets(today)
    except SQLAlchemyError:
        return {"archived": [], "failed": {}, "skipped": "intraday_series table missing"}
    due = [t for t in todo if _tries.get((*t, today), (0, 0.0))[0] < MAX_TRIES
           and _time.time() - _tries.get((*t, today), (0, 0.0))[1] >= RETRY_S]  # fmt: skip
    out: dict[str, Any] = {"archived": [], "failed": {}}
    for kind, sym in due:
        n, _ = _tries.get((kind, sym, today), (0, 0.0))
        _tries[(kind, sym, today)] = (n + 1, _time.time())
        try:
            series = await fetch(kind, sym)
        except Exception as e:
            out["failed"][sym] = f"{type(e).__name__}: {e}"[:200]
            continue
        if series.day != today:  # NSE still serves the previous session (or nothing): nothing to add today
            _tries[(kind, sym, today)] = (MAX_TRIES, _time.time())
            continue
        with session_scope() as s:
            store(s, series, now)
        out["archived"].append(sym)
    if out["failed"]:
        log.warning("intraday archive: %s", out["failed"])
    return out


async def live_fetch(kind: str, symbol: str) -> IntradaySeries:
    """NSE's series for an NSE symbol or index; BSE's (with BSE volume) for a "BSE:<scrip code>" key."""
    from finresearch.adapters.bse_equity import scrip_code_of
    from finresearch.adapters.nse import NseClient
    from finresearch.adapters.nse_intraday import equity_intraday, index_intraday

    code = scrip_code_of(symbol) if kind == "equity" else None
    if code is not None:
        from finresearch.adapters.bse import BseClient
        from finresearch.adapters.bse_intraday import bse_intraday

        async with BseClient() as bse:
            return await bse_intraday(bse, code)

    async with NseClient() as nse:
        return await (index_intraday(nse, symbol) if kind == "index" else equity_intraday(nse, symbol))


def reset_tries() -> None:
    _tries.clear()

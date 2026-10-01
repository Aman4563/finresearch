"""Daily ATM implied-volatility recorder (roadmap item 18).

After the close (from 15:50 IST) the monitor reads one option chain per underlying — NIFTY, BANKNIFTY, FINNIFTY and
every actively watched NSE stock that trades in F&O (BSE-only watches have no options) — and stores that day's ATM IV and 25-delta skew in `iv_history`.
Nothing historical is available from NSE, so IV rank and percentile start once 60 days have been recorded.

- The contract is the nearest expiry at least 7 days away, so same-week expiries (whose IV swings on time decay
  alone) do not add noise; the expiry used is stored with each row.
- The row's day is the chain's own timestamp date: on a holiday NSE serves the last session's chain, whose date is
  already recorded, so nothing new is written.
- One row per (symbol, day) (unique key, insert-or-ignore). A failed symbol is retried at most 3 times a day, 10
  minutes apart, to keep NSE traffic modest.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from finresearch.db import session_scope
from finresearch.db.models import IvHistory, Watch
from finresearch.fincalc.dates import to_ist
from finresearch.fincalc.volatility import StrikeIv, atm_iv, skew_25d

log = logging.getLogger(__name__)
INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY")
START = (15, 50)  # IST: NSE closes at 15:30; the chain settles a few minutes later
MIN_DTE = 7
RATE = 0.065
MAX_TRIES, RETRY_S = 3, 600
_tries: dict[tuple[str, date], tuple[int, float]] = {}
_missing_logged: list[bool] = []
_lots: dict[date, dict[str, dict[str, int]]] = {}  # the F&O lot file, fetched once per IST day
# failed lot-file fetches (count, last try) per day: retried like a symbol, never cached as "no F&O stocks today"
_lot_tries: dict[date, tuple[int, float]] = {}


def pick_expiry(expiries: list[date], today: date, min_dte: int = MIN_DTE) -> date | None:
    """The nearest expiry at least `min_dte` days away (else the furthest listed)."""
    later = sorted(e for e in expiries if e >= today)
    return next((e for e in later if (e - today).days >= min_dte), later[-1] if later else None)


def snapshot(chain, today: date) -> dict[str, Any] | None:
    """The iv_history row for one chain, or None when it has no ATM IV."""
    if chain.underlying is None:
        return None
    spot = float(chain.underlying)
    rows = [StrikeIv(float(r.strike), float(r.call.iv) if r.call and r.call.iv else None,
                     float(r.put.iv) if r.put and r.put.iv else None) for r in chain.rows]  # fmt: skip
    atm = atm_iv(rows, spot)
    if atm is None:
        return None
    row = next(r for r in rows if r.strike == atm[0])
    t = max((chain.expiry - today).days, 0.5) / 365
    skew = skew_25d(rows, spot, t, RATE)
    q = lambda x: None if x is None else Decimal(str(round(x, 3)))  # noqa: E731
    day = to_ist(chain.as_of).date() if chain.as_of else today
    return {"symbol": chain.symbol.upper(), "day": day, "expiry": chain.expiry, "underlying": chain.underlying,
            "atm_strike": Decimal(str(atm[0])), "atm_iv": q(atm[1]), "call_iv": q(row.call_iv), "put_iv": q(row.put_iv),
            "skew_25d": q(skew), "as_of": chain.as_of}  # fmt: skip


def symbols(lots: dict[str, dict[str, int]] | None) -> list[str]:
    """The indices plus every NSE stock watch that trades in F&O. BSE-only watches (Watch.exchange "BSE") are left out
    on purpose: stock options trade on NSE only, so a BSE-only stock has no option chain and no IV to record."""
    with session_scope() as s:
        watched = s.scalars(select(Watch.nse_symbol).where(Watch.active.is_(True), Watch.kind == "stock",
                                                           Watch.exchange != "BSE")).all()  # fmt: skip
    extra = sorted({w.upper() for w in watched if w and lots and w.upper() in lots})
    return [*INDEX_SYMBOLS, *(x for x in extra if x not in INDEX_SYMBOLS)]


async def record_iv(client_factory, now: datetime) -> dict[str, Any]:
    """Record today's ATM IV for every symbol not yet recorded. Returns {"recorded": [...], "failed": {...}}. Makes
    no network call once every symbol is recorded or out of tries for the day (the monitor ticks every minute)."""
    local = to_ist(now)
    today = local.date()
    if local.weekday() >= 5 or (local.hour, local.minute) < START:
        return {"recorded": [], "failed": {}, "skipped": "outside the after-close window"}
    try:
        with session_scope() as s:
            done = set(s.scalars(select(IvHistory.symbol).where(IvHistory.day == today)).all())
    except SQLAlchemyError:  # the iv_history migration has not run on this database yet
        if not _missing_logged:
            log.warning("IV history: table iv_history missing; run `alembic upgrade head`")
            _missing_logged.append(True)
        return {"recorded": [], "failed": {}, "skipped": "iv_history table missing"}
    lots = _lots.get(today)
    cands = [x for x in symbols(lots) if x not in done and _tries.get((x, today), (0, 0.0))[0] < MAX_TRIES]
    due = [x for x in cands if time.time() - _tries.get((x, today), (0, 0.0))[1] >= RETRY_S]
    lot_n, lot_at = _lot_tries.get(today, (0, 0.0))
    lots_due = lots is None and lot_n < MAX_TRIES and time.time() - lot_at >= RETRY_S
    if not due and not lots_due:
        return {"recorded": [], "failed": {}, "skipped": "nothing due"}
    out: dict[str, Any] = {"recorded": [], "failed": {}}
    async with client_factory() as f:
        if lots_due:
            _lot_tries[today] = (lot_n + 1, time.time())
            try:
                lots = await f.lot_sizes()
                _lots.clear()
                _lots[today] = lots
            except Exception as e:  # the indices still run; the stocks wait for the lot file's retry
                out["failed"]["lot sizes"] = f"{type(e).__name__}: {e}"[:200]
            cands = [x for x in symbols(lots) if x not in done]
            due = [x for x in cands if _tries.get((x, today), (0, 0.0))[0] < MAX_TRIES
                   and time.time() - _tries.get((x, today), (0, 0.0))[1] >= RETRY_S]  # fmt: skip
        for sym in due:
            n, _ = _tries.get((sym, today), (0, 0.0))
            _tries[(sym, today)] = (n + 1, time.time())
            try:
                expiries, _ = await f.contract_info(sym)
                expiry = pick_expiry(expiries, today)
                if expiry is None:
                    raise LookupError("no expiries")
                row = snapshot(await f.option_chain(sym, expiry), today)
                if row is None:
                    raise LookupError("no ATM IV in the chain")
            except Exception as e:
                out["failed"][sym] = f"{type(e).__name__}: {e}"[:200]
                continue
            with session_scope() as s:
                s.execute(
                    insert(IvHistory).values(**row).on_conflict_do_nothing(index_elements=["symbol", "day"])
                )
            # a holiday chain carries the last session's date: nothing new today, so no retry either
            _tries[(sym, today)] = (MAX_TRIES, time.time()) if row["day"] != today else _tries[(sym, today)]
            out["recorded"].append(sym)
    if out["failed"]:
        log.warning("IV history: %s", out["failed"])
    return out


def reset_tries() -> None:
    _tries.clear()
    _lots.clear()
    _lot_tries.clear()

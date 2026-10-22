"""The monitor's nightly joint P&L pass (#397 (b), portfolio.joint_pnl): one append-only `joint_pnl_day` row per
trading day, for the pre-registered forward test (evals/experiments/joint_risk/PREREG.md, ADDENDUM.md).

Due on trading days from RUN_AFTER (23:15 IST: after the portfolio's NAV pass at 23:00, so the day's mutual-fund NAVs
are in the value history). It waits until tonight's F&O marks (monitor.fno_marks) are written, NIFTY's close is stored
(monitor.index_eod) and the canonical value history reaches today; from DEADLINE (23:45 IST) it runs anyway, so a
missing input is stored as an unusable day with the reason instead of nothing. It builds the value history once with
NIFTYBEES (`Deps.joint_history`, the holdings' betas and own closes; cached closes, so only the new day is fetched).
A failed build fails the slot (retried, monitor.disclosures MAX_ATTEMPTS, RETRY_AFTER apart) and writes nothing.

Holiday policy, on purpose: fails closed (monitor.market_days). On a day whose holiday list is unknown nothing is
written, and the next stored day sees that session in between and is stored unusable with that reason, so no day is
silently explained over two sessions. Claimed in `alert_eval_slot` ("joint_pnl:<day>"); the slot's result holds
counts only, never a rupee figure. Analysis only: nothing here places an order. Local only: never sent to a model.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(23, 15)
DEADLINE = time(23, 45)
PREFIX = "joint_pnl:"
HISTORY_TIMEOUT_S = 600  # as monitor.portfolio_daily: the first build with NIFTYBEES reads years of closes


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if ist.time() < RUN_AFTER or not market_day(ist.date(), holidays, job="joint P&L"):
        return None
    return f"{PREFIX}{ist.date().isoformat()}"


def ready(day: date) -> bool:
    """Tonight's marks are written, NIFTY's close is stored and the value history reaches `day`."""
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, IndexEod
    from finresearch.portfolio import series

    with session_scope() as s:
        row = s.get(AlertEvalSlot, f"fno_marks:{day.isoformat()}")
        if row is None or (row.result or {}).get("status") != "done":
            return False
        if s.get(IndexEod, (day, "NIFTY")) is None:
            return False
        ser, _ = series.load(s)
        return ser is not None and day in ser.days


async def joint_pnl_step(deps: Any, now: datetime, holidays: set[date] | None = None) -> dict[str, Any]:
    from finresearch.db import session_scope
    from finresearch.monitor.disclosures import claim, finish
    from finresearch.portfolio import joint_pnl as jpl
    from finresearch.portfolio.history import holdings_from
    from finresearch.portfolio.report import load

    slot = due_slot(now, holidays)
    if slot is None:
        return {}
    day = to_ist(now).date()
    if to_ist(now).time() < DEADLINE and not ready(day):
        return {}
    if not claim(slot, now):
        return {}
    try:
        hist = None
        if deps.joint_history is not None:
            with session_scope() as s:
                hs = holdings_from(load(s))
            hist = await asyncio.wait_for(deps.joint_history(hs, day), HISTORY_TIMEOUT_S)
        if holidays is None:
            from finresearch.adapters.nse_holidays import known_trading_holidays

            holidays = known_trading_holidays(day) or set()
        with session_scope() as s:
            res = jpl.record(s, day, hist, holidays=holidays)
    except Exception as e:
        log.warning("joint P&L pass failed; retried later", exc_info=True)
        finish(slot, {"error": type(e).__name__}, now, failed=True)
        return {"joint_pnl": {"error": type(e).__name__}}
    finish(slot, res, now, failed=False)
    return {"joint_pnl": res}

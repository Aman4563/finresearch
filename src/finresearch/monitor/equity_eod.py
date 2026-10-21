"""The monitor's nightly full equity bhavcopy update (#373 (a), refseries.equity_eod): once per trading-day evening
from RUN_AFTER (IST), NSE's sec_bhavdata_full file for today (one GET, ~400 KB), plus any of the last LOOKBACK_DAYS
days missed while the Mac was off. When today's file is not there yet the run counts as failed and is retried
(monitor.disclosures MAX_ATTEMPTS, 30 minutes apart). Weekends and NSE holidays are skipped; an unknown holiday list
skips the run too (monitor.market_days fails closed). The night is claimed in `alert_eval_slot`
("equity_eod:<day>"), so two monitor processes never run it twice. Older history is the CLI's job (`finresearch
equity backfill`), outside market hours.

RUN_AFTER is half an hour before the stock peer build (monitor.stock_peers, 19:30), which reads today's closes from
this store when `Settings.peer_price_source` is "equity_eod". NSE's publication time for this file was not measured
[U]; the F&O bhavcopy of the same evening is stamped 17:56-19:17 IST (adapters.nse_fno_bhav).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(19, 0)
LOOKBACK_DAYS = 4


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if ist.time() < RUN_AFTER or not market_day(ist.date(), holidays, job="full equity bhavcopy"):
        return None
    return f"equity_eod:{ist.date().isoformat()}"


async def equity_eod_step(now: datetime, holidays: set[date] | None = None, **kw: Any) -> dict[str, Any]:
    """Claim tonight's slot and fetch the missing recent days. `kw` goes to refseries.equity_eod.backfill (tests)."""
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now, holidays)
    if slot is None or not claim(slot, now):
        return {}
    today = to_ist(now).date()
    try:
        from finresearch.refseries import equity_eod as store

        res = await store.backfill(today - timedelta(days=LOOKBACK_DAYS), today, today=today, holidays=holidays,
                                   now=lambda: now, **kw)  # fmt: skip
    except Exception as e:
        log.warning("full equity bhavcopy update failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"equity_eod": "failed"}
    pending = not loaded(today)
    summary = res | {"today_pending": pending}
    finish(slot, summary, now, failed=pending or bool(res["failed"]))
    return {"equity_eod": summary}


def loaded(day: date) -> bool:
    from finresearch.db import session_scope
    from finresearch.refseries.equity_eod import is_loaded

    with session_scope() as s:
        return is_loaded(s, day)

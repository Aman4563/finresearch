"""The monitor's nightly stock peer build (signals.stock_peers): once per weekday evening from RUN_AFTER (IST).

After the close the quote's price is the official close, so the stored peer prices are that day's closes. About
500 quotes plus 500 small history requests a night at NSE's polite rate (adapters.http: 2/s) and PAUSE_S between
stocks: roughly 15-20 minutes; results are re-read weekly per stock. Held and watched stocks outside the NIFTY 500
and their industry peers add a bounded number (signals.stock_peers module docstring: about 300-500 for 10 such
stocks). Weekends and NSE holidays are skipped (no new prices; an unknown holiday list skips too). Each run is
claimed in `alert_eval_slot` ("stock_peers:<day>"), so two monitor processes never run it twice; a failed run is
retried (monitor.disclosures.MAX_ATTEMPTS). Pages only read the stored file.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(
    19, 30
)  # NSE publishes the official close at about 16:00 IST; after 19:30 the quote is settled


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    """The night's slot from RUN_AFTER on trading days. NSE holidays are skipped (no new closes); an unknown holiday
    list skips the run too (monitor.market_days fails closed)."""
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if ist.time() < RUN_AFTER or not market_day(ist.date(), holidays, job="stock peers"):
        return None
    return f"stock_peers:{ist.date().isoformat()}"


async def stock_peers_step(now: datetime, holidays: set[date] | None = None) -> dict[str, Any]:
    from finresearch.monitor.disclosures import claim, finish
    from finresearch.signals import stock_peers

    slot = due_slot(now, holidays)
    if slot is None or not claim(slot, now):
        return {}
    try:
        res = await stock_peers.refresh(to_ist(now).date())
    except Exception as e:
        log.warning("stock peer build failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"stock_peers": "failed"}
    finish(slot, res, now, failed=False)
    return {"stock_peers": res}

"""The monitor's nightly incremental IPO history harvest (evals.ipo_history.harvest, #292): mainboard (EQ) then SME.

Once per trading-day evening from RUN_AFTER (IST), after NSE has published the day's bars. Only issues not yet
`complete` in ipo_history are fetched, and older partial rows only when they listed in the last RETRY_DAYS days, so a
night with no new listing costs one `public-past-issues` request per board. A new listing costs its issue page, one or
two history requests (EQ, then the T2T "BE" series when EQ has no listing-day bar) and the current Nifty chunk. Every
request goes through the shared per-host NSE rate limit (adapters.http.SharedSlots) at the harvester's 0.5 req/s.
NSE holidays and weekends are skipped; an unknown holiday list skips the run too (monitor.market_days fails closed).
Each night is claimed in `alert_eval_slot` ("ipo_harvest:<day>"), so two monitor processes never run it twice; a
failed run is retried (monitor.disclosures.MAX_ATTEMPTS). Re-running is harmless: rows are upserted by symbol, series
and issue start. The forecast resolver (signals.ledger.resolve_listing_gain) reads the listing bars it stores.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(20, 30)  # after the 19:30 stock peer build; NSE's daily history has the session by then
RETRY_DAYS = 10  # partial rows listed this recently are retried (the listing bar may be published late)
BOARDS = ("EQ", "SME")


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if ist.time() < RUN_AFTER or not market_day(ist.date(), holidays, job="IPO history harvest"):
        return None
    return f"ipo_harvest:{ist.date().isoformat()}"


async def run(today: date, *, client=None, session_factory=None) -> dict[str, Any]:
    """Both boards, one after the other; a failed board is reported and does not stop the other."""
    from finresearch.evals.ipo_history import harvest

    out: dict[str, Any] = {}
    failed = []
    for board in BOARDS:
        try:
            out[board] = await harvest(series=board, today=today, retry_days=RETRY_DAYS, client=client,
                                       session_factory=session_factory, log=log.info)  # fmt: skip
        except Exception as e:
            log.warning("IPO history harvest (%s) failed", board, exc_info=True)
            out[board] = {"error": f"{type(e).__name__}: {e}"[:300]}
            failed.append(board)
    out["failed"] = failed
    return out


async def ipo_harvest_step(now: datetime, holidays: set[date] | None = None, **kw: Any) -> dict[str, Any]:
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now, holidays)
    if slot is None or not claim(slot, now):
        return {}
    res = await run(to_ist(now).date(), **kw)
    finish(slot, res, now, failed=bool(res["failed"]))
    return {"ipo_harvest": res}

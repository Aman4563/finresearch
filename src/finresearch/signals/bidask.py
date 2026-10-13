"""Bid/ask for the trade plan's legs when the plan's own chain has none (#324). Groww's option chain carries no bid or
ask, so with Groww connected the entry range was always unknown. NSE's chain does: it is read for the plan's
symbol + expiry through the NSE client (adapters.nse_fno.NseFno.nse_option_chain: the shared polite client, NSE's
2 req/s budget) only when an option leg lacks a quote, and cached per symbol + expiry:

* in session (09:15-16:00 IST, adapters.groww_market.phase "open"/"closing"): reused for TTL_S seconds;
* out of session: the last chain read is reused until the next session opens (quotes do not move after the close);
  a chain read in session is refetched once after the close, then kept.

A failure is never cached and never raises: the entry range stays "unknown". The label names the source and the
chain's own timestamp ("bid/ask: NSE chain at 10:42"), with the date when it is not today's."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from finresearch.fincalc.dates import IST, to_ist

log = logging.getLogger(__name__)

TTL_S = 30.0
_CACHE: dict[tuple[str, date], tuple[Any, datetime]] = {}


def _live(now: datetime) -> bool:
    from finresearch.adapters.groww_market import phase

    return phase(now) in ("open", "closing")


def _fresh(fetched: datetime, now: datetime) -> bool:
    if (now - fetched).total_seconds() < TTL_S:
        return True
    if _live(now):
        return False
    from finresearch.adapters.groww_market import next_open

    # out of session: good until the next open, unless it was read in session (then once more after the close)
    return not _live(fetched) and next_open(fetched) > now


async def nse_chain(f: Any, symbol: str, expiry: date, now: datetime) -> Any | None:
    """NSE's option chain for symbol + expiry (cached as above), or None when it cannot be read."""
    key = (symbol.upper(), expiry)
    hit = _CACHE.get(key)
    if hit is not None and _fresh(hit[1], now):
        return hit[0]
    fetch = getattr(f, "nse_option_chain", None)
    if fetch is None:
        return None
    try:
        chain = await fetch(symbol, expiry)
    except Exception as e:
        log.info("NSE option chain for the bid/ask of %s %s failed: %s", symbol, expiry, type(e).__name__)
        return None
    if chain is None or not chain.rows:
        return None
    _CACHE[key] = (chain, now)
    return chain


def label(chain: Any, today: date, fetched: datetime | None = None) -> str:
    """'bid/ask: NSE chain at HH:MM' from the chain's own timestamp (the fetch time when it has none)."""
    at = chain.as_of or fetched
    if at is None:
        return f"bid/ask: {chain.source} chain (time unknown)"
    at = to_ist(at if at.tzinfo else at.replace(tzinfo=IST))
    day = "" if at.date() == today else f" on {at:%d-%b-%Y}"
    return f"bid/ask: {chain.source} chain at {at:%H:%M}{day}"


FAILED_NOTE = ("NSE's option chain could not be read for the bid/ask (the plan's chain has none): the entry range is "
               "unknown")  # fmt: skip

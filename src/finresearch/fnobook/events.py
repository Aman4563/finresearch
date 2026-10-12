"""The event strip on /fno and the dates the "event before expiry" exit rule reads (#308).

* Results: the next board meeting on financial results of a stock underlying, from the portfolio's corporate-events
  pass (`cache.EVENTS`, holdings only) or else NSE's board-meetings list for that symbol (read at most once a day per
  symbol). Indices have no results date.
* RBI monetary-policy dates: no source in the app yet. RBI_POLICY_DATES stays empty until a dated list is copied from
  an RBI press release with its URL; until then the strip says "no source" and, for an index (where RBI is the event
  that matters), the event rule reads unknown rather than "no event".
* Expiries: the contract's live expiries.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from finresearch.adapters.nse_fno import INDEX_NAMES

# (policy announcement date, source URL): empty until verified from an RBI press release (a decision for the user)
RBI_POLICY_DATES: tuple[tuple[date, str], ...] = ()
RBI_NO_SOURCE = "RBI policy dates: no source in the app yet"
NSE_BOARD_MEETINGS = "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"
_CACHE: dict[tuple[str, date], tuple[float, Any]] = {}
TTL_S = 6 * 3600


def is_index(symbol: str) -> bool:
    return symbol.upper() in INDEX_NAMES or symbol.upper() in ("SENSEX", "BANKEX")


def _cached_results(symbol: str, today: date) -> list[date] | None:
    """Results meeting dates from the portfolio events pass when it covers `symbol` today, else None."""
    try:
        from finresearch.db import session_scope
        from finresearch.portfolio import cache

        with session_scope() as s:
            ev = cache.read(s, cache.EVENTS) or {}
    except Exception:
        return None
    st = (ev.get("stocks") or {}).get(symbol.upper())
    if not st or ev.get("day") != today.isoformat():
        return None
    return [date.fromisoformat(m["day"]) for m in st.get("board_meetings") or [] if m.get("results")]


async def results_dates(symbol: str, today: date, sources: Any) -> tuple[list[date] | None, str]:
    """(upcoming results dates, source) for a stock; (None, reason) when they could not be read."""
    got = _cached_results(symbol, today)
    if got is not None:
        return sorted(d for d in got if d >= today), "NSE board meetings (portfolio events pass)"
    key = (symbol.upper(), today)
    hit = _CACHE.get(key)
    if hit is None or hit[0] < time.time():
        try:
            meetings = await sources.board_meetings(symbol.upper())
            val: Any = sorted({m.day for m in meetings if m.day and m.results and m.day >= today})
        except Exception as e:
            val = e
        _CACHE[key] = (time.time() + (TTL_S if not isinstance(val, Exception) else 600), val)
        hit = _CACHE[key]
    if isinstance(hit[1], Exception):
        return None, f"NSE board meetings could not be read ({type(hit[1]).__name__})"
    return hit[1], NSE_BOARD_MEETINGS


def rbi_dates(today: date) -> list[tuple[date, str]] | None:
    return [(d, u) for d, u in RBI_POLICY_DATES if d >= today] if RBI_POLICY_DATES else None


async def event_dates(symbol: str, today: date, sources: Any) -> list[tuple[date, str]] | None:
    """The dates the event exit rule reads; None = unknown (see the module docstring)."""
    rbi = rbi_dates(today)
    out = [(d, "RBI policy") for d, _ in rbi or []]
    if is_index(symbol):
        return out if rbi is not None else None
    res, _ = await results_dates(symbol, today, sources)
    if res is None:
        return None
    return out + [(d, "results") for d in res]


async def strip(symbol: str, today: date, sources: Any, expiries: list[date]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if is_index(symbol):
        results = {"status": "n/a", "note": "an index has no results date"}
    else:
        res, src = await results_dates(symbol, today, sources)
        results = ({"status": "unknown", "note": src} if res is None else
                   {"status": "ok" if res else "none", "next": res[0].isoformat() if res else None, "source": src,
                    "note": None if res else "no results board meeting announced yet"})  # fmt: skip
        for d in res or []:
            items.append({"day": d.isoformat(), "kind": "results", "title": f"{symbol.upper()} results (board "
                          "meeting)", "source": src})  # fmt: skip
    rbi = rbi_dates(today)
    for d, u in rbi or []:
        items.append({"day": d.isoformat(), "kind": "rbi", "title": "RBI monetary policy", "source": u})
    for e in expiries[:4]:
        items.append({"day": e.isoformat(), "kind": "expiry", "title": f"{symbol.upper()} expiry",
                      "source": "NSE contract info"})  # fmt: skip
    items.sort(key=lambda x: (x["day"], x["kind"]))
    return {"symbol": symbol.upper(), "today": today.isoformat(), "items": items, "results": results,
            "rbi": {"status": "ok", "source": "RBI press releases"} if rbi is not None
            else {"status": "no_source", "note": RBI_NO_SOURCE}}  # fmt: skip

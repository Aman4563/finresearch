"""The event strip on /fno and the dates the "event before expiry" exit rule reads (#308).

* Results: the next board meeting on financial results of a stock underlying, from the portfolio's corporate-events
  pass (`cache.EVENTS`, holdings only) or else NSE's board-meetings list for that symbol (read at most once a day per
  symbol). Indices have no results date.
* Tier-1 macro dates (RBI policy, Union Budget, general-election results): from the point-in-time event calendar
  (finresearch.eventcal, #367), only rows public by the end of `today`, and only when every tier-1 schedule is known
  through the window's end (`until`, default today + 92 days); otherwise unknown (None), never "no event". For an
  index (where these are the events that matter) an unknown calendar makes the event rule unknown.
* Expiries: the contract's live expiries.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any

from finresearch.adapters.nse_fno import INDEX_NAMES

RBI_NO_SOURCE = "Tier-1 event calendar (RBI policy, Budget, elections): not covered for this window"
LABELS = {
    "rbi_mpc": "RBI policy",
    "union_budget": "Union Budget",
    "general_election": "General-election results",
}
HORIZON = timedelta(days=92)  # the default window when the caller has no expiry: about a quarterly contract
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


def macro_dates(today: date, until: date | None = None) -> list[tuple[date, str, str, str]] | None:
    """(India session day, type, label, source URL) of tier-1 events from `today` to `until`, as known at the end of
    `today`; None when the calendar cannot be read or does not cover every tier-1 type through `until`."""
    from finresearch.db import session_scope
    from finresearch.eventcal import calendar as cal
    from finresearch.eventcal import store
    from finresearch.fincalc.dates import IST

    end = until or today + HORIZON
    try:
        with session_scope() as s:
            ev, cov = store.load(s)
    except Exception:
        return None
    as_of = datetime.combine(today, dtime.max, IST)
    if any((cal.covered_through(cov, k, as_of) or date.min) < end for k in cal.TIER1_TYPES):
        return None
    rows = [(cal.india_day(e), e.type, LABELS[e.type], e.source_url) for e in cal.known_at(ev, as_of)
            if e.tier == 1 and e.type in LABELS]  # fmt: skip
    return sorted(r for r in rows if today <= r[0])


async def event_dates(
    symbol: str, today: date, sources: Any, until: date | None = None
) -> list[tuple[date, str]] | None:
    """The dates the event exit rule reads up to `until` (the expiry); None = unknown (see the module docstring)."""
    macro = macro_dates(today, until)
    out = [(d, label) for d, _, label, _ in macro or []]
    if is_index(symbol):
        return out if macro is not None else None
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
    macro = macro_dates(today, max(expiries[:4], default=None))
    for d, kind, label, u in macro or []:
        items.append({"day": d.isoformat(), "kind": "rbi" if kind == "rbi_mpc" else "macro",
                      "title": "RBI monetary policy" if kind == "rbi_mpc" else label, "source": u})  # fmt: skip
    for e in expiries[:4]:
        items.append({"day": e.isoformat(), "kind": "expiry", "title": f"{symbol.upper()} expiry",
                      "source": "NSE contract info"})  # fmt: skip
    items.sort(key=lambda x: (x["day"], x["kind"]))
    return {"symbol": symbol.upper(), "today": today.isoformat(), "items": items, "results": results,
            "rbi": {"status": "ok", "source": "event calendar (#367): RBI, indiabudget.gov.in, ECI"} if macro is not None
            else {"status": "no_source", "note": RBI_NO_SOURCE}}  # fmt: skip

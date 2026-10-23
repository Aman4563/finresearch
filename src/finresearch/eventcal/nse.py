"""NSE's F&O circulars, read by the event calendar's nightly refresh (#367): expiry-day revisions are reported for the
owner to review, and index constituent changes are kept as dated facts that confirm NIFTY's review windows (#481).

Source (official, read 23-Oct-2026): the circulars page https://www.nseindia.com/resources/exchange-communication-circulars
answers through `GET /api/circulars?fromDate=DD-MM-YYYY&toDate=DD-MM-YYYY` with `{"data": [{"cirDate": "20261023",
"circDisplayNo": "NSE/FAOP/76610", "fileDept": "FAOP", "sub": "...", "circFilelink": "https://nsearchives..."}]}`.
One request a night covers the last LOOKBACK_DAYS; no circular body is downloaded.
* Expiry days: F&O subjects about a revised, changed or shifted expiry day become notices (circular number, date,
  subject, link). They are policy text (NSE/FAOP/68589 of 17-Jun-2025 and 68747 of 25-Jun-2025 moved the weekly
  expiry from Thursday to Tuesday from 1-Sep-2025, with an annexure of illustrations), not a date that can be read
  mechanically; a holiday that moves one expiry is already in the contracts' expiry dates and NSE's holiday list; the
  rare weekday-regime changes are curated by hand (inventory/regimes.py).
* Index changes: NSE's "Change in the Constituents of Indices" circular is dated the day of the close it concerns
  (NSE/FAOP/76610, 29-Sep-2026, w.e.f. 30-Sep), so it gives no advance notice but confirms the change. Its date, number
  and link are stored as an index_rebalance row (context, tier 2): it confirms the expected NIFTY review window
  (eventcal.rules) within MATCH_DAYS, moving the window to the circular's day when they differ, and is otherwise a
  dated change of its own. A row is never back-dated: it is known from the night it is read.

Terms: NSE's website terms (data_rights._NSE_TERMS); the owner accepted this dataset on 23-Oct-2026 (#481) under the
#362 rules (personal, non-distributed use, polite access).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

PAGE = "https://www.nseindia.com/resources/exchange-communication-circulars"
API = "/api/circulars"
LOOKBACK_DAYS = 10
_EXPIRY = re.compile(r"expir", re.I)
_MOVED = re.compile(r"revis|chang|shift|modif|resched|postpon|prepon", re.I)
_CONSTITUENTS = re.compile(r"\b(?:constituents?|replacements?)\b", re.I)
_INDEX = re.compile(r"\bind(?:ex|ices)\b", re.I)
MATCH_DAYS = 10  # an expected NIFTY review window this close to a circular's date is the one it confirms


class NseCircularsError(RuntimeError):
    pass


def expiry_notices(listing: Any) -> list[str]:
    """One line per F&O circular about a revised, changed or shifted expiry day, in listing order."""
    out = []
    for r in _rows(listing):
        if not isinstance(r, dict) or r.get("fileDept") != "FAOP":
            continue
        sub = str(r.get("sub") or "").strip()
        if _EXPIRY.search(sub) and _MOVED.search(sub):
            day = str(r.get("cirDate") or "")
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}" if re.fullmatch(r"\d{8}", day) else day
            out.append(
                f"{r.get('circDisplayNo') or r.get('circNumber')} of {day}: {sub} ({r.get('circFilelink')})"
            )
    return out


def _rows(listing: Any) -> list[Any]:
    rows = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(rows, list):
        raise NseCircularsError("NSE circulars answered without its list ('data')")
    return rows


def index_changes(listing: Any) -> list[tuple[date, str, str, str]]:
    """(date, number, subject, link) of each F&O circular about index constituents, in listing order."""
    out = []
    for r in _rows(listing):
        if not isinstance(r, dict) or r.get("fileDept") != "FAOP":
            continue
        sub = " ".join(str(r.get("sub") or "").split())
        if not (_CONSTITUENTS.search(sub) and _INDEX.search(sub)) or _EXPIRY.search(sub):
            continue
        day, link = str(r.get("cirDate") or ""), r.get("circFilelink")
        if not re.fullmatch(r"\d{8}", day) or not isinstance(link, str) or not link.startswith("https://"):
            raise NseCircularsError(
                f"NSE index-change circular without a date or link: {r.get('circDisplayNo')}"
            )
        number = str(r.get("circDisplayNo") or r.get("circNumber"))
        out.append((date(int(day[:4]), int(day[4:6]), int(day[6:])), number, sub, link))
    return out


def confirmations(known, changes: list[tuple[date, str, str, str]], now: datetime):
    """The index_rebalance rows and window moves for circulars not yet stored (see the module docstring)."""
    from finresearch.eventcal.calendar import TITLES, Event
    from finresearch.eventcal.refresh import Move
    from finresearch.eventcal.rules import NIFTY_EXPECTED

    windows = [d for d, t in known.standing.get("index_rebalance", {}).items() if NIFTY_EXPECTED in t]
    events, moves = [], []
    for day, number, sub, link in changes:
        if link in known.urls:
            continue
        near = sorted((abs((w - day).days), w) for w in windows if abs((w - day).days) <= MATCH_DAYS)
        what = f"{number} of {day.isoformat()} ({sub[:70]})"
        if near:
            title = f"{TITLES['index_rebalance']}: NIFTY semi-annual review confirmed by {what}"
            if near[0][1] != day:
                moves.append(Move("index_rebalance", "IN", near[0][1], day))
        else:
            title = f"{TITLES['index_rebalance']}: {what}"
        events.append(Event(type="index_rebalance", tier=2, scope="IN", day=day, at=None, title=title,
                            source_url=link, known_since=now, known_since_url=link))  # fmt: skip
    return events, moves


async def read(known, now: datetime, *, nse=None):
    """One listing request for the last LOOKBACK_DAYS: expiry-day revisions as notices, index changes as rows."""
    from finresearch.adapters.http import PoliteClient
    from finresearch.adapters.nse import NseClient
    from finresearch.eventcal.refresh import Found
    from finresearch.fincalc.dates import to_ist

    today: date = to_ist(now).date()
    own = nse is None
    nse = nse or NseClient(PoliteClient(cache_dir=None), warmup_url=PAGE)
    try:
        listing, _ = await nse.get_json(API, {"fromDate": (today - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%m-%Y"),
                                              "toDate": today.strftime("%d-%m-%Y")})  # fmt: skip
    finally:
        if own:
            await nse.aclose()
    events, moves = confirmations(known, index_changes(listing), now)
    return Found("nse_circulars", urls=[PAGE], notices=expiry_notices(listing), events=events, moves=moves)

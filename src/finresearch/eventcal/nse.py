"""NSE's F&O circulars, read by the event calendar's nightly refresh (#367) only to report expiry-day revisions for the
owner to review. Nothing is stored from them.

Source (official, read 23-Oct-2026): the circulars page https://www.nseindia.com/resources/exchange-communication-circulars
answers through `GET /api/circulars?fromDate=DD-MM-YYYY&toDate=DD-MM-YYYY` with `{"data": [{"cirDate": "20261023",
"circDisplayNo": "NSE/FAOP/76610", "fileDept": "FAOP", "sub": "...", "circFilelink": "https://nsearchives..."}]}`.
One request a night covers the last LOOKBACK_DAYS; F&O subjects about a revised, changed or shifted expiry day become
notices (circular number, date, subject, link). Why notices and not calendar rows: the expiry-day circulars are policy
text (NSE/FAOP/68589 of 17-Jun-2025 and 68747 of 25-Jun-2025 moved the weekly expiry from Thursday to Tuesday from
1-Sep-2025, with an annexure of illustrations), not a date that can be read mechanically; a holiday that moves one
expiry is already in the contracts' own expiry dates and NSE's holiday list; and the rare weekday-regime changes are
curated by hand (inventory/regimes.py). Index rebalances are not read here: NSE's "Change in the Constituents of
Indices" circular is dated the day of the close it concerns (NSE/FAOP/76610, 29-Sep-2026, w.e.f. 30-Sep), so it gives no
notice; eventcal.nifty_indices reads NSE Indices' own press releases instead.

Terms: NSE's website terms (data_rights._NSE_TERMS), not re-read for this dataset: automated collection prohibited.
Collection waits for the owner's decision (data_rights.PENDING["nse_circulars"]).
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


class NseCircularsError(RuntimeError):
    pass


def expiry_notices(listing: Any) -> list[str]:
    """One line per F&O circular about a revised, changed or shifted expiry day, in listing order."""
    rows = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(rows, list):
        raise NseCircularsError("NSE circulars answered without its list ('data')")
    out = []
    for r in rows:
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


async def read(known, now: datetime, *, nse=None):
    """One listing request for the last LOOKBACK_DAYS; the expiry-day revisions in it as notices."""
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
    return Found("nse_circulars", urls=[PAGE], notices=expiry_notices(listing))

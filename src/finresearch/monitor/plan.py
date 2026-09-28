"""Deterministic schedule of checks for a watched IPO (all times IST).

Dates after the close follow SEBI's T+3 timeline counted in exchange days (allotment T+1, listing T+3). They are
expected dates: the listing date is replaced by NSE's own listing date once the quote shows it. Exchange holidays
are not in the calendar yet, so a holiday shifts the real dates by a day; the listing check retries until NSE lists
the stock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from finresearch.fincalc.dates import add_business_days, bidding_dates, business_days_between, ist_datetime
from finresearch.fincalc.ipo import lock_in_schedule

# bidding-day checks; the last one follows the 17:00 close so it records the final day's book
SUBSCRIPTION_TIMES = ((10, 30), (12, 0), (13, 30), (15, 0), (16, 0), (17, 15))
LISTING_TIMES = ((10, 15, "open"), (15, 45, "close"))
ALLOTMENT_TIME = (19, 0)
LOCKIN_TIME = (9, 0)


@dataclass(frozen=True)
class Slot:
    kind: str
    slot: str
    due_at: datetime
    params: dict[str, Any] = field(default_factory=dict)


def expected_dates(close: date) -> tuple[date, date]:
    """(allotment, listing) = (T+1, T+3) exchange days after the close."""
    return add_business_days(close, 1), add_business_days(close, 3)


def plan(symbol: str, open_date: date, close_date: date, allotment: date, listing: date,
         anchor_shares=None) -> list[Slot]:  # fmt: skip
    n_days = business_days_between(open_date, close_date) + 1
    days = bidding_dates(open_date, n_days)
    out: list[Slot] = []
    for d in days:
        for h, m in SUBSCRIPTION_TIMES:
            final = d == close_date and (h, m) == SUBSCRIPTION_TIMES[-1]
            out.append(Slot("subscription", f"{symbol}:subscription:{d}:{h:02d}{m:02d}", ist_datetime(d, h, m),
                            {"final": final, "day": days.index(d) + 1}))  # fmt: skip
    out.append(Slot("allotment", f"{symbol}:allotment:{allotment}", ist_datetime(allotment, *ALLOTMENT_TIME)))
    for h, m, which in LISTING_TIMES:
        out.append(Slot("listing", f"{symbol}:listing:{listing}:{which}", ist_datetime(listing, h, m),
                        {"which": which}))  # fmt: skip
    for ev in lock_in_schedule(allotment, anchor_shares=anchor_shares):
        if ev.holder.startswith("anchor"):
            out.append(Slot("lockin", f"{symbol}:lockin:{ev.unlock_date}:{ev.holder}",
                            ist_datetime(ev.unlock_date, *LOCKIN_TIME),
                            {"holder": ev.holder, "unlock_date": ev.unlock_date.isoformat(),
                             "shares": str(ev.shares) if ev.shares is not None else None, "basis": ev.basis}))  # fmt: skip
    return sorted(out, key=lambda s: s.due_at)


def last_slot(slots: list[Slot]) -> datetime:
    return max(s.due_at for s in slots) + timedelta(days=1)

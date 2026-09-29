"""Deterministic schedule of checks for a watched IPO (all times IST).

Dates after the close follow SEBI's T+3 timeline counted in working days (allotment T+1, listing T+3), skipping
NSE's trading and settlement holidays from the cached holiday master. They are still expected dates: the listing
date is replaced by NSE's own listing date once the quote shows it, and the listing check retries until then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from finresearch.fincalc.dates import add_business_days, bidding_dates, business_days_between, ist_datetime
from finresearch.fincalc.ipo import LockInEvent, add_months, lock_in_schedule

# bidding-day checks; the last one follows the 17:00 close so it records the final day's book
SUBSCRIPTION_TIMES = ((10, 30), (12, 0), (13, 30), (15, 0), (16, 0), (17, 15))
LISTING_TIMES = ((10, 15, "open"), (15, 45, "close"))
ALLOTMENT_TIME = (19, 0)
LOCKIN_TIME = (9, 0)
SIX_MONTHS = 6
SIX_MONTH_NOTE = ("This is usually the largest unlock (often most of the pre-issue capital); check the share count "
                  "in the RHP's capital structure and the exchange's lock-in notice.")  # fmt: skip
STOCK_DAILY_TIME = (16, 30)  # after the close, once NSE has published the day's prices and filings


@dataclass(frozen=True)
class Slot:
    kind: str
    slot: str
    due_at: datetime
    params: dict[str, Any] = field(default_factory=dict)


def expected_dates(close: date, holidays: set[date] | None = None) -> tuple[date, date]:
    """(allotment, listing) = (T+1, T+3) working days after the close, skipping NSE trading and settlement
    holidays (default: the cached NSE lists)."""
    if holidays is None:
        from finresearch.adapters.nse_holidays import settlement_holidays

        holidays = settlement_holidays()
    allot = add_business_days(close, 1, holidays)
    listing = add_business_days(close, 3, holidays)
    return allot, listing


def plan(symbol: str, open_date: date, close_date: date, allotment: date, listing: date,
         anchor_shares=None, holidays: set[date] | None = None, listed: frozenset[str] = frozenset()) -> list[Slot]:  # fmt: skip
    """``listed``: listing events ("open", "close") already recorded, which are not planned again when NSE's
    listing date replaces the expected one."""
    if holidays is None:
        from finresearch.adapters.nse_holidays import trading_holidays

        holidays = trading_holidays()
    n_days = business_days_between(open_date, close_date, holidays) + 1
    days = bidding_dates(open_date, n_days, holidays)
    out: list[Slot] = []
    for d in days:
        for h, m in SUBSCRIPTION_TIMES:
            final = d == close_date and (h, m) == SUBSCRIPTION_TIMES[-1]
            out.append(Slot("subscription", f"{symbol}:subscription:{d}:{h:02d}{m:02d}", ist_datetime(d, h, m),
                            {"final": final, "day": days.index(d) + 1}))  # fmt: skip
    out.append(Slot("allotment", f"{symbol}:allotment:{allotment}", ist_datetime(allotment, *ALLOTMENT_TIME)))
    for h, m, which in LISTING_TIMES:
        if which in listed:
            continue
        out.append(Slot("listing", f"{symbol}:listing:{listing}:{which}", ist_datetime(listing, h, m),
                        {"which": which}))  # fmt: skip
    events = lock_in_schedule(allotment, anchor_shares=anchor_shares)
    lockins = [ev for ev in events if ev.holder.startswith("anchor")]
    # promoter holding above the minimum and other pre-IPO shares unlock together after 6 months: one alert
    six = [ev for ev in events if ev.unlock_date == add_months(allotment, SIX_MONTHS) and ev not in lockins]
    if six:
        lockins.append(LockInEvent("promoter (above the minimum) and pre-IPO shareholder", six[0].unlock_date,
                                   None, "; ".join(ev.basis for ev in six)))  # fmt: skip
    for ev in lockins:
        note = SIX_MONTH_NOTE if ev.unlock_date == add_months(allotment, SIX_MONTHS) else None
        out.append(Slot("lockin", f"{symbol}:lockin:{ev.unlock_date}:{ev.holder}",
                        ist_datetime(ev.unlock_date, *LOCKIN_TIME),
                        {"holder": ev.holder, "unlock_date": ev.unlock_date.isoformat(),
                         "shares": str(ev.shares) if ev.shares is not None else None, "basis": ev.basis,
                         **({"note": note} if note else {})}))  # fmt: skip
    return sorted(out, key=lambda s: s.due_at)


def last_slot(slots: list[Slot]) -> datetime:
    return max(s.due_at for s in slots) + timedelta(days=1)


def plan_stock(symbol: str, start: date, end: date, holidays: set[date] | None = None) -> list[Slot]:
    """One after-close check per exchange trading day in [start, end] for a watched listed stock."""
    from finresearch.fincalc.dates import is_business_day

    if holidays is None:
        from finresearch.adapters.nse_holidays import trading_holidays

        holidays = trading_holidays()
    out, d = [], start
    while d <= end:
        if is_business_day(d, holidays):
            out.append(Slot("stock_daily", f"{symbol}:stock_daily:{d}", ist_datetime(d, *STOCK_DAILY_TIME)))
        d += timedelta(days=1)
    return out

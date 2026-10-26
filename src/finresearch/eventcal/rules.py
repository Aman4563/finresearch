"""Event dates by published rule (#481): nothing is collected to make them. The owner's decision of 23-Oct-2026 dropped
MoSPI's release calendar and NSE Indices' press releases as sources; these rules replace them.

India CPI (context, tier 2). MoSPI's CPI press release states the next release date and the rule behind it; the
release of 12-Oct-2026 (CPI on base 2024=100 for September 2026, CPI_RULE_URL, read 23-Oct-2026) says "CPI for
October 2026 will be released on 12th November, 2026 (Thursday) or the next working day in case of 12th being a
holiday" and is "embargoed ... till 4:00 PM of 12th October, 2026". So each month's release is the 12th, or the next
working day, at 16:00 IST: after NSE's close, so calendar.india_day puts it on the next session. A working day here is
a weekday that is not an NSE trading holiday (the NSE/Mumbai list the app already holds, adapters.nse_holidays, as the
owner chose): central-government holidays are not the same list, so the rows say "expected". For a year whose list is
not cached only weekends are skipped and the title says "approximate". Rows run a year ahead from tomorrow (from today
before 16:00) and are regenerated nightly; a date that moves when NSE's list arrives withdraws its old row
(refresh.Replace). A release counts as published once MoSPI's CPI API (mospi_cpi, already read for the goal planner,
no new dataset) shows the month: `confirm_cpi` asks it only on nights when an expected release has come.

India GDP: left out. MoSPI's GDP press notes give the next release as a date ("The next release of ...", Q1 2026-27
note of 31-Aug-2026) "as per the release calendar", not a rule, and the 2026-27 dates (5-Jun, 31-Aug, 30-Nov-2026,
7-Jan, 26-Feb-2027) follow none; with the calendar dropped there is no sanctioned automated source.

NIFTY's semi-annual review (context, tier 2: index_rebalance). NSE Indices' NIFTY 50 methodology ("Index
Re-balancing", in its factsheet, NIFTY_METHOD_URL): "Index is re-balanced on semi-annual basis. The cut-off date is
January 31 and July 31 of each year ... Four weeks prior notice is given to market from the date of change." Its Index
Reconstitution Calendar (NIFTY_RULE_URL) puts the semi-annual changes on the last working day of March and September.
Both were read through search results on 23-Oct-2026, never fetched: NSE Indices' terms forbid automated collection,
and the owner's decision (#481) is to cite the rule without fetching niftyindices.com. NSE's own F&O circular agrees:
NSE/FAOP/76610 of 29-Sep-2026, "w.e.f. September 30, 2026 (close of September 29, 2026)". So each window is one row on
the session before the month's last NSE trading day, where index funds trade, titled "expected window": the rule
gives the window, not the changes, and a review may change no NIFTY 50 member. NSE's F&O circular on the change
confirms or moves it (eventcal.nse). Holidays, horizon and the replacement of stored future rows are as for CPI; rows
start tomorrow, so a window confirmed today is never regenerated.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta

# MoSPI's CPI press release for September 2026 (12-Oct-2026), which states the rule quoted above
CPI_RULE_URL = ("https://www.mospi.gov.in/uploads/latestreleasesfiles/1791801549227-Press%20Release%20for%20the%20"
                "month%20of%20September,%202026.pdf")  # fmt: skip
CPI_DAY = 12
CPI_TIME = time(16, 0)  # IST: "embargoed ... till 4:00 PM"
HORIZON_DAYS = 366
CONFIRM_DAYS = 45  # an expected release is checked against the API for this long after its day
EXPECTED = "expected by MoSPI's rule"
PUBLISHED = "published"
HolidaysFor = Callable[[date], set[date] | None]


def _holidays(day: date) -> set[date] | None:
    from finresearch.adapters.nse_holidays import known_trading_holidays

    return known_trading_holidays(day)


def next_working_day(d: date, holidays: set[date] | None) -> date:
    """`d` itself or the first later day that is neither a weekend nor in `holidays`."""
    while d.weekday() >= 5 or d in (holidays or ()):
        d += timedelta(days=1)
    return d


def cpi_release(year: int, month: int, holidays_for: HolidaysFor = _holidays) -> tuple[date, bool]:
    """The CPI release day in `month` of `year` (the previous month's index) and whether it is exact (False: that
    year's NSE holiday list is unknown, so only weekends were skipped)."""
    twelfth = date(year, month, CPI_DAY)
    holidays = holidays_for(twelfth)
    return next_working_day(twelfth, holidays), holidays is not None


def _month_before(d: date) -> date:
    return date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)


def _at(d: date) -> datetime:
    from finresearch.fincalc.dates import IST

    return datetime.combine(d, CPI_TIME, IST)


async def read_cpi(known, now: datetime, *, holidays_for: HolidaysFor = _holidays):
    """The CPI releases of the next year by MoSPI's rule, and a replacement of the stored ones in that span."""
    from finresearch.eventcal.calendar import Event
    from finresearch.eventcal.refresh import Found, Replace
    from finresearch.fincalc.dates import to_ist

    ist = to_ist(now)
    first = ist.date() if ist.time() < CPI_TIME else ist.date() + timedelta(days=1)
    last = ist.date() + timedelta(days=HORIZON_DAYS)
    found = Found("india_cpi_rule", replaces=[Replace("india_cpi", "IN", first - timedelta(days=1), last)])
    y, m = first.year, first.month
    while date(y, m, 1) <= last:
        day, exact = cpi_release(y, m, holidays_for)
        if first <= day <= last:
            title = (f"India CPI release for {_month_before(day):%b %Y} ({EXPECTED}: the 12th or the next working day, "
                     "16:00 IST; NSE holidays as working days)"
                     + ("" if exact else f"; approximate: the {day.year} NSE holiday list is unknown"))  # fmt: skip
            found.events.append(Event(type="india_cpi", tier=2, scope="IN", day=day, at=_at(day), title=title,
                                      source_url=CPI_RULE_URL, known_since=now, known_since_url=CPI_RULE_URL))  # fmt: skip
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return found


async def _fetch_cpi(today: date):
    from finresearch.adapters.mospi_cpi import MospiCpiClient

    async with MospiCpiClient() as c:  # its own disk cache, shared with the goal planner
        return await c.fetch(today=today)


async def confirm_cpi(known, now: datetime, *, fetch: Callable[[date], Awaitable] | None = None):
    """Mark expected CPI releases that have come as published once MoSPI's CPI API shows their month. The API is
    read only on nights when such a release is due (about one a month); a month still missing after its release day
    is a notice and the row stays expected."""
    from finresearch.adapters.mospi_cpi import BASE
    from finresearch.eventcal.calendar import Event
    from finresearch.eventcal.refresh import Found
    from finresearch.fincalc.dates import to_ist

    today = to_ist(now).date()
    found = Found("mospi_cpi")
    due = sorted(d for d, title in known.standing.get("india_cpi", {}).items()
                 if EXPECTED in title and today - timedelta(days=CONFIRM_DAYS) <= d <= today)  # fmt: skip
    if not due:
        return found
    data = await (fetch or _fetch_cpi)(today)
    url = BASE + "getCPIData"
    found.urls.append(url)
    for d in due:
        month = _month_before(d)
        if data.new_last is not None and data.new_last >= month:
            title = f"India CPI release for {month:%b %Y} ({PUBLISHED}: in MoSPI's CPI API on {today.isoformat()})"
            found.events.append(Event(type="india_cpi", tier=2, scope="IN", day=d, at=_at(d), title=title,
                                      source_url=url, known_since=now, known_since_url=url))  # fmt: skip
        elif d < today:
            found.notices.append(f"MoSPI's CPI API does not show {month:%b %Y} CPI yet; MoSPI's rule expected it on "
                                 f"{d.isoformat()} at 16:00 IST")  # fmt: skip
    return found


# ---- NIFTY's semi-annual review windows (cited, never fetched: NSE Indices' terms, #481)

NIFTY_RULE_URL = "https://www.niftyindices.com/resources/index-rebalancing-schedule"
NIFTY_METHOD_URL = "https://www.niftyindices.com/Factsheet/ind_nifty50.pdf"
REVIEW_MONTHS = (3, 9)  # effective on the last working day of March and September
NIFTY_EXPECTED = "NIFTY semi-annual review, expected window"


def nifty_review(year: int, month: int, holidays_for: HolidaysFor = _holidays) -> tuple[date, date, bool]:
    """(effective day, the session before it whose close index funds trade at, exact) for a review month. Not exact:
    that year's NSE holiday list is unknown, so only weekends were skipped."""
    from finresearch.fincalc.dates import add_business_days

    last = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    holidays = holidays_for(last)
    effective = last
    while effective.weekday() >= 5 or effective in (holidays or ()):
        effective -= timedelta(days=1)
    return effective, add_business_days(effective, -1, holidays or set()), holidays is not None


async def read_nifty(known, now: datetime, *, holidays_for: HolidaysFor = _holidays):
    """The NIFTY review windows of the next year by NSE Indices' rule, and a replacement of the stored ones."""
    from finresearch.eventcal.calendar import TITLES, Event
    from finresearch.eventcal.refresh import Found, Replace
    from finresearch.fincalc.dates import to_ist

    today = to_ist(now).date()
    first, last = today + timedelta(days=1), today + timedelta(days=HORIZON_DAYS)
    found = Found("nifty_review_rule", replaces=[Replace("index_rebalance", "IN", today, last)])
    for y in range(first.year, last.year + 1):
        for m in REVIEW_MONTHS:
            effective, flow, exact = nifty_review(y, m, holidays_for)
            if not first <= flow <= last:
                continue
            title = (f"{TITLES['index_rebalance']}: {NIFTY_EXPECTED} (effective {effective:%d-%b-%Y}, last working day "
                     f"of {effective:%b}; funds trade at this close)"
                     + ("" if exact else f"; approximate: {effective.year} NSE holiday list unknown"))  # fmt: skip
            found.events.append(Event(type="index_rebalance", tier=2, scope="IN", day=flow, at=None, title=title,
                                      source_url=NIFTY_RULE_URL, known_since=now, known_since_url=NIFTY_RULE_URL))  # fmt: skip
    return found

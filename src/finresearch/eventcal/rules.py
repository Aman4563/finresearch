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


async def confirm_cpi(known, now: datetime, *, fetch: Callable[[date], Awaitable] = _fetch_cpi):
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
    data = await fetch(today)
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

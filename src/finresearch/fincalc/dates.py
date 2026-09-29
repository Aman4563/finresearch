"""Indian market calendar helpers: weekends, business days, IPO bidding days, IST.

Holidays are supplied by the caller (``holidays`` = a collection of ``date``) — this module
does not ship an exchange holiday list. IST is a fixed UTC+05:30 offset (India has no DST).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def now_ist() -> datetime:
    """Current time as an aware IST datetime."""
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    """Convert an aware datetime to IST. Raises ValueError for naive datetimes (ambiguous)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime; attach a tzinfo first (see ist_datetime)")
    return dt.astimezone(IST)


def add_years(d: date, years: int) -> date:
    """The same calendar day ``years`` years later (or earlier with a negative ``years``); 29 Feb falls back to
    28 Feb in a non-leap year."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def fiscal_year(d: date) -> int:
    """The Indian financial year (April-March) a day falls in, named by its end year: 30-Jun-2026 -> 2027."""
    return d.year + 1 if d.month >= 4 else d.year


def fiscal_quarter_label(period_end: date) -> str:
    """'Q1 FY27' for a quarter ending 30-Jun-2026 (Apr-Jun is Q1 of the April-March year)."""
    q = (period_end.month - 4) % 12 // 3 + 1
    return f"Q{q} FY{fiscal_year(period_end) % 100:02d}"


def ist_datetime(d: date, hour: int = 0, minute: int = 0) -> datetime:
    """Aware IST datetime for a wall-clock time on ``d`` (e.g. UPI mandate cutoff 17:00)."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def today_ist() -> date:
    """Today's date in India (differs from UTC date between 00:00 and 05:30 IST)."""
    return datetime.now(UTC).astimezone(IST).date()


def is_weekend(d: date) -> bool:
    """Saturday or Sunday."""
    return d.weekday() >= 5


def is_business_day(d: date, holidays: Collection[date] | None = None) -> bool:
    """Not a weekend and not in ``holidays``."""
    return not is_weekend(d) and not (holidays and d in holidays)


def next_business_day(d: date, holidays: Collection[date] | None = None) -> date:
    """First business day strictly after ``d``."""
    d += timedelta(days=1)
    while not is_business_day(d, holidays):
        d += timedelta(days=1)
    return d


def add_business_days(d: date, n: int, holidays: Collection[date] | None = None) -> date:
    """Move ``n`` business days forward (``n > 0``) or backward (``n < 0``) from ``d``.

    ``n == 0`` returns ``d`` unchanged even if it is not a business day. Counting T+3 listing:
    ``add_business_days(close_date, 3, holidays)``.
    """
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if is_business_day(d, holidays):
            remaining -= 1
    return d


def business_days_between(start: date, end: date, holidays: Collection[date] | None = None) -> int:
    """Business days in the half-open interval ``(start, end]``; negative if ``end < start``."""
    if end < start:
        return -business_days_between(end, start, holidays)
    count, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if is_business_day(d, holidays):
            count += 1
    return count


def bidding_dates(open_date: date, n_days: int, holidays: Collection[date] | None = None) -> list[date]:
    """The ``n_days`` bidding dates of an IPO opening on ``open_date``, skipping weekends and
    holidays. Raises ValueError if ``open_date`` itself is not a business day."""
    if n_days < 1:
        raise ValueError("n_days must be >= 1")
    if not is_business_day(open_date, holidays):
        raise ValueError(f"open date {open_date} is not a business day")
    out = [open_date]
    while len(out) < n_days:
        out.append(next_business_day(out[-1], holidays))
    return out


def bidding_day_number(
    open_date: date, on_date: date, holidays: Collection[date] | None = None
) -> int | None:
    """Which bidding day ``on_date`` is (1-based) for an IPO opening on ``open_date``.

    Weekends and holidays are not bidding days, so an IPO opening Fri 25 Sep 2026 has
    Day 2 = Mon 28 Sep and Day 3 = Tue 29 Sep. Returns None if ``on_date`` is before the
    open date or is itself a non-business day. Does not know the close date — compare with
    the RHP's issue period yourself.
    """
    if not is_business_day(open_date, holidays):
        raise ValueError(f"open date {open_date} is not a business day")
    if on_date < open_date or not is_business_day(on_date, holidays):
        return None
    return business_days_between(open_date, on_date, holidays) + 1

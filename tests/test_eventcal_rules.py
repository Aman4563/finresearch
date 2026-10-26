"""#481: event dates by published rule, with no collection. India CPI comes out on the 12th of each month, or the next
working day, at 16:00 IST (MoSPI's CPI press releases), confirmed once MoSPI's CPI API (mospi_cpi, already collected)
shows the month. NIFTY's semi-annual review windows follow NSE Indices' published methodology. Expected dates below
are hand-derived (weekdays from the calendar module), with synthetic holiday lists."""

from __future__ import annotations

from datetime import date, datetime

import pytest
import respx

from finresearch.adapters.mospi_cpi import CpiData
from finresearch.eventcal import calendar as cal
from finresearch.eventcal import refresh, rules
from finresearch.fincalc.dates import IST

NO_HOLIDAYS = lambda _d: set()  # noqa: E731 - the year's NSE list is known and has no holiday in the way
UNKNOWN = lambda _d: None  # noqa: E731 - the year's NSE list is not cached


@pytest.mark.parametrize(("year", "month", "holidays", "want"), [
    (2026, 11, set(), date(2026, 11, 12)),  # Thursday: the 12th itself (MoSPI: "12th November, 2026 (Thursday)")
    (2026, 12, set(), date(2026, 12, 14)),  # Saturday -> Monday
    (2026, 4, set(), date(2026, 4, 13)),  # Sunday -> Monday (MoSPI's calendar: "Released on 13th April, 2026")
    (2026, 7, set(), date(2026, 7, 13)),  # Sunday -> Monday (the June-2026 release: "till 4:00 PM of 13th July")
    (2027, 3, {date(2027, 3, 12)}, date(2027, 3, 15)),  # a Friday holiday -> Monday
    (2026, 11, {date(2026, 11, 12), date(2026, 11, 13)}, date(2026, 11, 16)),  # Thu and Fri holidays -> Monday
])  # fmt: skip
def test_cpi_is_released_on_the_12th_or_the_next_working_day(year, month, holidays, want):
    assert rules.cpi_release(year, month, lambda _d: holidays) == (want, True)


async def test_cpi_with_an_unknown_holiday_list_skips_weekends_only_and_says_so():
    assert rules.cpi_release(2027, 3, UNKNOWN) == (date(2027, 3, 12), False)
    assert rules.cpi_release(2027, 6, UNKNOWN) == (date(2027, 6, 14), False)  # Saturday -> Monday still
    found = await _cpi(datetime(2027, 2, 13, 21, 0, tzinfo=IST), UNKNOWN)
    assert all("approximate" in e.title for e in found.events)


async def _cpi(now: datetime, holidays_for=NO_HOLIDAYS) -> refresh.Found:
    return await rules.read_cpi(refresh.Known(urls=frozenset(), covered={}), now, holidays_for=holidays_for)


async def test_the_rule_lists_a_year_of_releases_at_16_00_ist_from_tomorrow():
    now = datetime(2026, 10, 24, 21, 0, tzinfo=IST)
    found = await _cpi(now)
    days = [e.day for e in found.events]
    assert days == [date(2026, 11, 12), date(2026, 12, 14), date(2027, 1, 12), date(2027, 2, 12), date(2027, 3, 12),
                    date(2027, 4, 12), date(2027, 5, 12), date(2027, 6, 14), date(2027, 7, 12), date(2027, 8, 12),
                    date(2027, 9, 13), date(2027, 10, 12)]  # fmt: skip
    first = found.events[0]
    assert (first.type, first.tier, first.scope) == ("india_cpi", 2, "IN")
    assert first.at == datetime(2026, 11, 12, 16, 0, tzinfo=IST) and cal.india_day(first) == date(
        2026, 11, 13
    )
    assert "Oct 2026" in first.title and "expected" in first.title and first.source_url == rules.CPI_RULE_URL
    # a date that moves when NSE's holiday list changes withdraws its old row: the stored future is replaced
    assert found.replaces == [refresh.Replace("india_cpi", "IN", date(2026, 10, 24), date(2027, 10, 25))]
    assert found.coverage == [] and found.urls == []  # nothing was read


async def test_a_release_day_is_listed_only_until_16_00():
    found = await _cpi(datetime(2026, 11, 12, 10, 0, tzinfo=IST))
    assert found.events[0].day == date(2026, 11, 12) and found.replaces[0].after == date(2026, 11, 11)
    # after the release the nightly run leaves that row alone, so its confirmation (confirm_cpi) is never undone
    found = await _cpi(datetime(2026, 11, 12, 21, 0, tzinfo=IST))
    assert found.events[0].day == date(2026, 12, 14) and found.replaces[0].after == date(2026, 11, 12)


# ---- confirmation: MoSPI's CPI API (mospi_cpi) shows the month; read only when an expected release has come

EXPECTED = "India CPI release for Oct 2026 (expected by MoSPI's rule)"


def _cpi_data(new_last: date | None) -> CpiData:
    months = {date(2026, 1, 1): 3.1} | ({new_last: 3.4} if new_last else {})
    return CpiData(old={"general": {date(2025, 12, 1): 190.2}, "health": {date(2025, 12, 1): 195.0},
                        "education": {date(2025, 12, 1): 182.4}},
                   new_yoy={"general": months, "health": months, "education": months}, old_last=date(2025, 12, 1),
                   new_last=new_last, fetched_at=None, urls=["https://api.mospi.gov.in/api/cpi/getCPIData"])  # fmt: skip


def _fetcher(new_last: date | None, calls: list[date]):
    async def fetch(today: date) -> CpiData:
        calls.append(today)
        return _cpi_data(new_last)

    return fetch


def _standing(**titles: str) -> refresh.Known:
    days = {date.fromisoformat(k.removeprefix("d").replace("_", "-")): t for k, t in titles.items()}
    return refresh.Known(urls=frozenset(), covered={}, standing={"india_cpi": days})


async def test_no_request_until_an_expected_release_has_come():
    calls: list[date] = []
    found = await rules.confirm_cpi(_standing(d2026_11_12=EXPECTED), datetime(2026, 10, 24, 21, 0, tzinfo=IST),
                                    fetch=_fetcher(date(2026, 10, 1), calls))  # fmt: skip
    assert calls == [] and found.events == [] and found.notices == []


async def test_a_release_is_confirmed_when_the_api_shows_its_month():
    calls: list[date] = []
    night = datetime(2026, 11, 12, 21, 0, tzinfo=IST)
    found = await rules.confirm_cpi(
        _standing(d2026_11_12=EXPECTED), night, fetch=_fetcher(date(2026, 10, 1), calls)
    )
    assert calls == [date(2026, 11, 12)]
    [e] = found.events
    assert (e.type, e.day, e.at) == (
        "india_cpi",
        date(2026, 11, 12),
        datetime(2026, 11, 12, 16, 0, tzinfo=IST),
    )
    assert (
        "Oct 2026" in e.title
        and "published" in e.title
        and "2026-11-12" in e.title
        and "expected" not in e.title
    )


async def test_a_month_still_missing_a_day_later_is_a_notice_and_stays_expected():
    calls: list[date] = []
    standing = _standing(d2026_11_12=EXPECTED)
    same_night = await rules.confirm_cpi(standing, datetime(2026, 11, 12, 21, 0, tzinfo=IST),
                                         fetch=_fetcher(date(2026, 9, 1), calls))  # fmt: skip
    assert same_night.events == [] and same_night.notices == []  # the API may lag the 16:00 release
    later = await rules.confirm_cpi(standing, datetime(2026, 11, 13, 21, 0, tzinfo=IST),
                                    fetch=_fetcher(date(2026, 9, 1), calls))  # fmt: skip
    assert later.events == [] and len(later.notices) == 1
    assert "Oct 2026" in later.notices[0] and "2026-11-12" in later.notices[0]


async def test_a_confirmed_release_is_not_asked_again():
    calls: list[date] = []
    done = "India CPI release for Oct 2026 (published: in MoSPI's CPI API on 2026-11-12)"
    found = await rules.confirm_cpi(_standing(d2026_11_12=done), datetime(2026, 11, 20, 21, 0, tzinfo=IST),
                                    fetch=_fetcher(date(2026, 10, 1), calls))  # fmt: skip
    assert calls == [] and found.events == []


async def test_the_cpi_rule_makes_no_request():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        await rules.read_cpi(
            refresh.Known(urls=frozenset(), covered={}), datetime(2026, 10, 24, 21, 0, tzinfo=IST)
        )
    assert not mock.calls


# ---- NIFTY's semi-annual review windows by NSE Indices' rule, confirmed by NSE's F&O circular (eventcal.nse)


@pytest.mark.parametrize(("year", "month", "holidays", "effective", "flow"), [
    # NSE/FAOP/76610 of 29-Sep-2026: "w.e.f. September 30, 2026 (close of September 29, 2026)"
    (2026, 9, set(), date(2026, 9, 30), date(2026, 9, 29)),
    (2027, 3, set(), date(2027, 3, 31), date(2027, 3, 30)),
    (2025, 3, {date(2025, 3, 31)}, date(2025, 3, 28), date(2025, 3, 27)),  # Monday 31st a holiday: Friday 28th
    (2024, 3, {date(2024, 3, 29)}, date(2024, 3, 28), date(2024, 3, 27)),  # weekend 30-31, holiday Friday 29th
])  # fmt: skip
def test_a_review_takes_effect_on_the_months_last_working_day_and_funds_trade_the_close_before(
    year, month, holidays, effective, flow
):
    assert rules.nifty_review(year, month, lambda _d: holidays) == (effective, flow, True)


def test_a_review_with_an_unknown_holiday_list_skips_weekends_only():
    assert rules.nifty_review(2028, 3, UNKNOWN) == (date(2028, 3, 31), date(2028, 3, 30), False)


async def test_the_rule_lists_the_review_windows_of_the_next_year():
    now = datetime(2026, 10, 24, 21, 0, tzinfo=IST)
    found = await rules.read_nifty(refresh.Known(urls=frozenset(), covered={}), now, holidays_for=NO_HOLIDAYS)
    assert [e.day for e in found.events] == [
        date(2027, 3, 30),
        date(2027, 9, 29),
    ]  # the closes funds trade at
    e = found.events[0]
    assert (e.type, e.tier, e.scope, e.at) == ("index_rebalance", 2, "IN", None)
    assert "expected window" in e.title and "31-Mar-2027" in e.title and e.source_url == rules.NIFTY_RULE_URL
    assert found.replaces == [
        refresh.Replace("index_rebalance", "IN", date(2026, 10, 24), date(2027, 10, 25))
    ]
    approx = await rules.read_nifty(refresh.Known(urls=frozenset(), covered={}), now, holidays_for=UNKNOWN)
    assert [e.day for e in approx.events] == [date(2027, 3, 30), date(2027, 9, 29)]
    for e in found.events + approx.events:
        assert len(e.title) <= 200  # calendar_event.title is String(200)
    assert all("approximate" in e.title for e in approx.events)


async def test_the_nifty_rule_makes_no_request():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        await rules.read_nifty(
            refresh.Known(urls=frozenset(), covered={}), datetime(2026, 10, 24, 21, 0, tzinfo=IST)
        )
    assert not mock.calls

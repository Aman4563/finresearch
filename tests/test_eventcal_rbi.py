"""#367 refresh: RBI's MPC schedule and rescheduling notices, read from hand-made copies of rbi.org.in's press-release
month listing and press-release pages (tests/fixtures/eventcal, synthetic dates)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.http import PoliteClient
from finresearch.eventcal import rbi, refresh
from finresearch.fincalc.dates import IST

FIX = Path(__file__).parent / "fixtures" / "eventcal"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_listing_rows_and_the_two_notice_kinds():
    rows = rbi.parse_listing(_read("rbi_listing.html"))
    assert [p for p, _ in rows] == [90002, 90001, 90000]
    # "Scheduled Banks' Statement" is not an MPC notice; only the schedule matches
    assert [(p, rbi.notice_kind(t)) for p, t in rows if rbi.notice_kind(t)] == [(90002, "schedule")]
    resched = rbi.parse_listing(_read("rbi_listing_resched.html"))
    assert [(p, rbi.notice_kind(t)) for p, t in resched] == [(90100, "reschedule"), (90099, None)]


def test_listing_form_carries_the_postback_fields_for_a_month():
    form = rbi.listing_form(_read("rbi_listing.html"), year=2027, month=3)
    assert (
        form["__VIEWSTATE"] == "/wEPfakeViewState==" and form["__EVENTVALIDATION"] == "/wEdfakeValidation=="
    )
    assert form["hdnYear"] == "2027" and form["hdnMonth"] == "3" and form["UsrFontCntr$btn"] == ""


def test_listing_form_without_a_viewstate_is_an_error():
    with pytest.raises(rbi.RbiError):
        rbi.listing_form("<html>blocked</html>", year=2027, month=3)


def test_schedule_decision_day_is_each_meeting_last_day():
    got = rbi.parse_schedule(_read("rbi_schedule.html"))
    assert got.fy == (2027, 2028)
    # independently: the last day of each listed meeting (the September one ends on 1 October)
    assert got.days == [date(2027, 4, 7), date(2027, 6, 4), date(2027, 8, 5), date(2027, 10, 1), date(2027, 12, 8),
                        date(2028, 2, 9)]  # fmt: skip
    assert got.through == date(2028, 3, 31)


def test_schedule_with_a_date_outside_its_year_or_too_few_meetings_is_refused():
    bad = _read("rbi_schedule.html").replace("February 7-9, 2028", "February 7-9, 2029")
    with pytest.raises(rbi.RbiError):
        rbi.parse_schedule(bad)
    short = _read("rbi_schedule.html")
    for row in ("April 5, 6 and 7, 2027", "June 2, 3 and 4, 2027", "August 3 &#8211; 5, 2027"):
        short = short.replace(row, "")
    with pytest.raises(rbi.RbiError):
        rbi.parse_schedule(short)


def test_reschedule_reads_old_and_new_last_days_and_ignores_the_cited_release_date():
    got = rbi.parse_reschedule(_read("rbi_resched.html"))
    assert (got.old, got.new) == (date(2027, 6, 4), date(2027, 6, 5))


def test_reschedule_without_two_distinct_dates_is_refused():
    with pytest.raises(rbi.RbiError):
        rbi.parse_reschedule(
            _read("rbi_resched.html").replace("June 3 &#8211; 5,\n2027", "June 2, 3 and 4, 2027")
        )


# ---- the nightly read through PoliteClient (respx; no network)

QUIET = "<html><form><input type='hidden' name='__VIEWSTATE' value='x' /></form><table><tr><td><a class='link2' " \
        "href=BS_PressReleaseDisplay.aspx?prid=80000>Result of the 7-day VRR auction</a></td></tr></table></html>"  # fmt: skip


async def _no_sleep(_s: float) -> None:
    return None


def _http() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


def _known(covered: date | None = None, urls: tuple[str, ...] = ()) -> refresh.Known:
    return refresh.Known(urls=frozenset(urls), covered={"rbi_mpc": covered} if covered else {})


def _mock(mock, months: dict[int, str], releases: dict[int, str]):
    for prid, body in releases.items():  # before the listing route, which ignores the query
        mock.get(rbi.RELEASE.format(prid)).mock(return_value=httpx.Response(200, text=body))
    mock.get(rbi.PAGE).mock(return_value=httpx.Response(200, text=_read("rbi_listing.html")))

    def listing(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, text=months.get(int(form["hdnMonth"]), QUIET))

    return mock.post(rbi.PAGE).mock(side_effect=listing)


def _months(post) -> list[tuple[str, str]]:
    return sorted((f["hdnYear"], f["hdnMonth"]) for f in
                  (dict(httpx.QueryParams(c.request.content.decode())) for c in post.calls))  # fmt: skip


async def test_read_finds_the_new_schedule_in_this_or_last_month():
    now = datetime(2027, 3, 25, 21, 0, tzinfo=IST)
    with respx.mock(assert_all_called=False) as mock:
        post = _mock(mock, {3: _read("rbi_listing.html")}, {90002: _read("rbi_schedule.html")})
        found = await rbi.read(_known(date(2027, 3, 31)), now, http=_http())
    assert _months(post) == [("2027", "2"), ("2027", "3")]
    assert [e.day for e in found.events] == rbi.parse_schedule(_read("rbi_schedule.html")).days
    assert {(e.type, e.tier, e.scope, e.source_url) for e in found.events} == {
        ("rbi_mpc", 1, "IN", rbi.RELEASE.format(90002))}  # fmt: skip
    assert [(c.type, c.through, c.source_url) for c in found.coverage] == [
        ("rbi_mpc", date(2028, 3, 31), rbi.RELEASE.format(90002))]  # fmt: skip


async def test_read_skips_a_release_already_stored():
    now = datetime(2027, 3, 25, 21, 0, tzinfo=IST)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, {3: _read("rbi_listing.html")}, {})
        found = await rbi.read(_known(date(2028, 3, 31), (rbi.RELEASE.format(90002),)), now, http=_http())
        assert not any("prid=" in str(c.request.url) for c in mock.calls if c.request.method == "GET")
    assert found.events == [] and found.coverage == []


async def test_read_widens_to_february_and_march_when_this_year_is_not_covered():
    now = datetime(2027, 4, 10, 21, 0, tzinfo=IST)  # FY 2027-28 began; coverage ends 31-Mar-2027
    with respx.mock(assert_all_called=False) as mock:
        post = _mock(mock, {}, {})
        await rbi.read(_known(date(2027, 3, 31)), now, http=_http())
    assert _months(post) == [("2027", "2"), ("2027", "3"), ("2027", "4")]


async def test_read_turns_a_rescheduling_notice_into_a_move():
    now = datetime(2027, 5, 21, 21, 0, tzinfo=IST)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, {5: _read("rbi_listing_resched.html")}, {90100: _read("rbi_resched.html")})
        found = await rbi.read(_known(date(2028, 3, 31)), now, http=_http())
    assert found.moves == [refresh.Move("rbi_mpc", "IN", date(2027, 6, 4), date(2027, 6, 5))]
    assert [(e.day, e.source_url) for e in found.events] == [(date(2027, 6, 5), rbi.RELEASE.format(90100))]
    assert found.coverage == []


async def test_a_listing_without_releases_or_a_refusal_is_an_error():
    now = datetime(2027, 3, 25, 21, 0, tzinfo=IST)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, {3: "<html>Access denied</html>"}, {})
        with pytest.raises(rbi.RbiError):
            await rbi.read(_known(date(2027, 3, 31)), now, http=_http())
    with respx.mock(assert_all_called=False) as mock:
        mock.get(rbi.PAGE).mock(return_value=httpx.Response(403, text="no"))
        with pytest.raises(rbi.RbiError):
            await rbi.read(_known(date(2027, 3, 31)), now, http=_http())


def test_an_unscheduled_meeting_title_is_not_read_as_the_schedule():
    assert rbi.notice_kind("Unscheduled meeting of the Monetary Policy Committee") is None
    assert rbi.notice_kind("Schedule of Monetary Policy Committee Meetings for 2021-22") == "schedule"


async def test_a_new_schedule_withdraws_no_stored_meeting():
    # tier 1: a date only a notice can withdraw (a stale extra date is flagged, a wrongly dropped one would be missed)
    now = datetime(2027, 3, 25, 21, 0, tzinfo=IST)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, {3: _read("rbi_listing.html")}, {90002: _read("rbi_schedule.html")})
        found = await rbi.read(_known(date(2027, 3, 31)), now, http=_http())
    assert found.replaces == [] and found.moves == []

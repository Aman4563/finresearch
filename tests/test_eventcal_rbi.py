"""#367 refresh: RBI's MPC schedule and rescheduling notices, read from hand-made copies of rbi.org.in's press-release
month listing and press-release pages (tests/fixtures/eventcal, synthetic dates)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finresearch.eventcal import rbi

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

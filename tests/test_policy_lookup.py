"""#492: the risk-free chain uses the hand-typed RBI policy-rate table plus the policy rates read from RBI's RSS feed
(fincalc.policy_feed, passed in as an `Observed`). The table (rows to 07-Oct-2026, checked through REVIEWED,
23-Oct-2026) stays the source for history and wins on any day it covers; a newer observation that states the repo
rate extends it, labelled with its item and the time it was seen. The "possibly stale" flag clears by itself once the
scheduled meeting is observed. Expected values are hand-computed: y the printed rate, z = (365/91) ln(1 + y 91/365)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from finresearch.fincalc import riskfree as R
from finresearch.fincalc.dates import IST
from finresearch.fincalc.policy_feed import Observation, Observed, merge

URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid={}"
RES = "Monetary Policy Statement, {} Resolution of the Monetary Policy Committee {}"
DEC_T = RES.format("2026-27", "December 2 to 4, 2026")
SEEN = datetime(2026, 12, 4, 12, 0, tzinfo=IST)


def ob(prid: int, decided: date, repo: str | None, sdf: str | None = None, msf: str | None = None, *,
       title: str = DEC_T, seen: datetime = SEEN) -> Observation:  # fmt: skip
    def d(x: str | None) -> Decimal | None:
        return None if x is None else Decimal(x)

    return Observation(decided=decided, repo=d(repo), sdf=d(sdf), msf=d(msf), title=title, url=URL.format(prid),
                       seen=seen)  # fmt: skip


HIKE = ob(91004, date(2026, 12, 4), "0.0575", "0.055", "0.06")  # 5.75 / 5.50 / 6.00 %
CUT = ob(91006, date(2027, 2, 5), "0.055", "0.0525", "0.0575", title=RES.format("2026-27", "February 3 to 5, 2027"),
         seen=datetime(2027, 2, 5, 12, 0, tzinfo=IST))  # fmt: skip
# the event calendar: the 2026-27 schedule's meetings after the table's review, complete through 31-Mar-2027
CAL = (date(2026, 12, 4), date(2027, 2, 5))
COV = date(2027, 3, 31)
Z_575, Z_550 = 0.0570917481, 0.0546263231  # (365/91) ln(1 + y 91/365) for y = 5.75 % and 5.50 %


def a(day: date, *obs: Observation, cal=CAL, cov=COV, before: bool = False, entries=()) -> dict:
    return R.answer(day, 91 / 365, entries, before=before, observed=Observed(tuple(obs), cal, cov))


def test_a_decision_after_the_table_extends_it_and_says_where_it_came_from():
    got = a(date(2026, 12, 10), HIKE)
    assert got["tier"] == "proxy" and got["yield"] == pytest.approx(0.0575) and got["as_of"] == "2026-12-04"
    assert got["rate"] == pytest.approx(Z_575, abs=1e-9)
    assert got["source"] == (f"RBI repo-rate proxy: repo 5.75 % in force from 04-Dec-2026 (from RBI's RSS feed, item "
                             f"{DEC_T}, seen 04-Dec-2026 12:00 IST)")  # fmt: skip
    assert got["url"] == URL.format(91004) and got["stale"] is False
    assert (got["policy"]["floor"], got["policy"]["msf"]) == (pytest.approx(0.055), pytest.approx(0.06))


def test_the_old_rate_holds_until_the_decision_day():
    assert a(date(2026, 12, 3), HIKE)["yield"] == pytest.approx(0.055)
    # known at the start of the decision day: the day's own decision is not used, and nothing is stale yet
    early = a(date(2026, 12, 4), HIKE, before=True)
    assert early["yield"] == pytest.approx(0.055) and early["stale"] is False


def test_the_table_wins_on_a_day_it_covers_and_a_disagreement_is_reported():
    wrong = ob(91999, date(2026, 10, 7), "0.0575", title=RES.format("2026-27", "October 5 to 7, 2026"))
    got = a(date(2026, 10, 20), wrong)
    assert (
        got["yield"] == pytest.approx(0.055) and "RSS" not in got["source"]
    )  # the typed 07-Oct-2026 row: 5.50 %
    (note,) = merge((wrong,)).disagreements
    assert "07-Oct-2026" in note and "5.75 %" in note and "5.50 %" in note and URL.format(91999) in note


def test_an_observation_agreeing_with_the_table_is_no_disagreement():
    # 05-Aug-2026 kept 5.25 / 5.00 / 5.50 %, the rates in force since the typed 05-Dec-2025 row
    hold = ob(
        91001, date(2026, 8, 5), "0.0525", "0.05", "0.055", title=RES.format("2026-27", "August 3 to 5, 2026")
    )
    assert merge((hold,)).disagreements == ()
    assert merge((hold,)).rows[-1].effective == date(2026, 10, 7)  # the table is not extended backwards


def test_the_stale_flag_clears_once_the_scheduled_meeting_is_read_and_returns_at_the_next():
    unread = a(date(2026, 12, 10))
    assert (
        unread["stale"] is True
        and "04-Dec-2026" in unread["note"]
        and unread["yield"] == pytest.approx(0.055)
    )
    assert a(date(2026, 12, 10), HIKE)["stale"] is False
    assert a(date(2027, 2, 4), HIKE)["stale"] is False  # the day before the next scheduled decision
    feb = a(date(2027, 2, 5), HIKE)  # 05-Feb-2027 is scheduled and not read yet
    assert feb["stale"] is True and "05-Feb-2027" in feb["note"] and feb["yield"] == pytest.approx(0.0575)
    both = a(date(2027, 2, 10), HIKE, CUT)
    assert both["stale"] is False and both["yield"] == pytest.approx(0.055)
    assert both["rate"] == pytest.approx(Z_550, abs=1e-9)


def test_a_meeting_read_without_its_repo_rate_keeps_the_flag():
    blind = ob(91004, date(2026, 12, 4), None)  # seen, but the text stated no figure: unknown, never a rate
    got = a(date(2026, 12, 10), blind)
    assert got["stale"] is True and got["yield"] == pytest.approx(0.055)


def test_past_the_calendars_coverage_the_answer_stays_possibly_stale():
    got = a(date(2027, 4, 1), HIKE, CUT)  # the calendar knows no meeting after 31-Mar-2027
    assert got["stale"] is True and "31-Mar-2027" in got["note"]
    # without the event calendar only the table's next meeting (04-Dec-2026) is known: nothing after it is vouched for
    assert a(date(2026, 12, 10), HIKE, cal=(), cov=None)["stale"] is True


def test_a_rescheduled_meeting_in_the_calendar_replaces_the_tables_next_date():
    moved = ob(
        91004, date(2026, 12, 5), "0.0575", "0.055", "0.06"
    )  # the calendar moved 04-Dec to 05-Dec-2026
    got = a(date(2026, 12, 10), moved, cal=(date(2026, 12, 5), date(2027, 2, 5)))
    assert got["stale"] is False and got["yield"] == pytest.approx(0.0575)


def test_a_repo_only_statement_extends_the_repo_rate_and_leaves_the_corridor_unknown():
    apr = ob(91008, date(2027, 4, 7), "0.055", title=RES.format("2027-28", "April 5 to 7, 2027"))
    got = a(date(2027, 4, 10), HIKE, CUT, apr, cal=(*CAL, date(2027, 4, 7)), cov=date(2028, 3, 31))
    assert got["yield"] == pytest.approx(0.055) and got["as_of"] == "2027-04-07" and got["stale"] is False
    assert (got["policy"]["floor"], got["policy"]["msf"]) == (None, None)


def test_a_conflicting_later_item_never_replaces_the_first_and_is_named():
    later = ob(91012, date(2026, 12, 4), "0.06", title=f"{DEC_T} (revised)", seen=datetime(2026, 12, 4, 16, 30,
               tzinfo=IST))  # fmt: skip
    got = a(date(2026, 12, 10), HIKE, later)
    assert got["yield"] == pytest.approx(0.0575)  # the first item seen stays in use
    assert URL.format(91012) in got["note"] and "6.00 %" in got["note"]
    assert len(merge((HIKE, later)).conflicts) == 1


def test_owner_entries_shift_by_a_change_read_from_the_feed():
    typed = R.OwnerRate(date(2026, 10, 14), 91, Decimal("0.057281"), "typed from RBI's auction result")
    got = a(date(2026, 12, 10), HIKE, entries=[typed])
    # 5.7281 % at repo 5.50 %, +25 bp after the 04-Dec-2026 hike read from the feed: 5.9781 %
    assert (
        got["tier"] == "owner"
        and got["shift_bp"] == 25
        and got["yield"] == pytest.approx(0.059781, abs=1e-12)
    )


def test_the_monthly_series_uses_the_feeds_decision_from_its_day():
    months = R.month_rates(date(2026, 12, 1), date(2026, 12, 31), observed=Observed((HIKE,), CAL, COV))
    # 1-3 Dec at 5.50 %, 4-31 Dec at 5.75 %: (3 x 0.055 + 28 x 0.0575) / 31
    assert months[date(2026, 12, 1)].y91 == pytest.approx(0.0572580645, abs=1e-10)

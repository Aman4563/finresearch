"""#479: the risk-free chain without FBIL (fincalc.riskfree): an owner-entered rate, else the RBI repo-rate proxy, else
unknown. Expected values are hand-computed from the stated conventions (simple Actual/365 yields y for D days;
continuous z = ln(1 + y·D/365)/(D/365)) or quoted from the research note, never by calling the code under test."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import riskfree as R

OCT23 = date(2026, 10, 23)  # repo 5.50 % since the 07-Oct-2026 hike


def z(y: float, days: int) -> float:
    return math.log(1 + y * days / 365) / (days / 365)


def test_conventions_match_the_research_notes_worked_example():
    # RBI's 14-Oct-2026 91-day cut-off 5.7281 % is 5.6876 % continuous and 5.8525 % effective annual (the note
    # computed them from the price 98.5920, i.e. 5.72814 %; the typed 5.7281 % gives 5.85244 %)
    assert R.continuous(0.057281, 91) == pytest.approx(0.056876, abs=5e-7)
    assert R.effective_annual(0.057281, 91) == pytest.approx(0.058525, abs=1e-6)
    assert R.continuous(0.055, 1) == pytest.approx(365 * math.log(1 + 0.055 / 365), abs=1e-12)


def test_the_91_day_proxy_on_a_known_date():
    a = R.answer(OCT23, 91 / 365)
    assert a["known"] is True and a["tier"] == "proxy" and a["fallback"] is False
    assert a["tenor_days"] == 91 and a["yield"] == pytest.approx(0.055)
    assert a["rate"] == pytest.approx(0.05462632, abs=1e-8)  # (365/91) ln(1 + 0.055 x 91/365), by hand
    assert a["rate"] == pytest.approx(z(0.055, 91), abs=1e-12)
    assert a["as_of"] == "2026-10-07"  # the decision in force
    assert a["source"].startswith("RBI repo-rate proxy") and "5.50 %" in a["source"]
    assert "rbi.org.in" in a["url"]


def test_a_week_is_the_repo_rate_at_seven_days():
    a = R.answer(OCT23, 7 / 365)
    assert a["tenor_days"] == 7 and a["rate"] == pytest.approx(z(0.055, 7), abs=1e-12)


def test_tenors_beyond_91_days_are_held_flat_and_say_so():
    a = R.answer(OCT23, 1.0)  # 365 days
    assert a["yield"] == pytest.approx(0.055) and a["rate"] == pytest.approx(math.log(1.055), abs=1e-12)
    assert "term" in a["note"]  # the caveat: no term spread is added beyond 91 days


def test_before_the_table_start_is_unknown_with_a_reason():
    a = R.answer(date(2014, 12, 31), 0.25)
    assert a["known"] is False and a["rate"] is None and a["tier"] is None
    assert "01-Jan-2015" in a["note"]


def test_before_excludes_a_decision_announced_that_day():
    a = R.answer(date(2026, 10, 7), 91 / 365, before=True)
    assert a["yield"] == pytest.approx(0.0525) and a["as_of"] != "2026-10-07"


# ------------------------------------------------------------------ the owner-entered tier
def own(day: date, tenor: int, pct: str) -> R.OwnerRate:
    return R.OwnerRate(day, tenor, Decimal(pct) / 100, "typed from RBI's auction result")


def test_an_owner_rate_is_used_unchanged_while_the_repo_has_not_moved():
    a = R.answer(date(2026, 10, 20), 91 / 365, [own(date(2026, 10, 14), 91, "5.7281")])
    assert a["tier"] == "owner" and a["yield"] == pytest.approx(0.057281)
    assert a["rate"] == pytest.approx(0.056876, abs=5e-7) and a["as_of"] == "2026-10-14"
    assert a["shift_bp"] == 0


def test_an_owner_rate_is_shifted_by_the_repo_change_since_its_date():
    # 30-Sep-2026 cut-off 5.5199 % at repo 5.25 %; the 07-Oct-2026 hike adds 25 bp: 5.7699 % on 23-Oct
    a = R.answer(OCT23, 91 / 365, [own(date(2026, 9, 30), 91, "5.5199")])
    assert a["tier"] == "owner" and a["shift_bp"] == 25
    assert a["yield"] == pytest.approx(0.057699, abs=1e-12) and a["rate"] == pytest.approx(z(0.057699, 91))
    assert "+25 bp" in a["note"]


def test_an_owner_rate_dated_after_the_day_is_not_used():
    a = R.answer(date(2026, 10, 13), 91 / 365, [own(date(2026, 10, 14), 91, "5.7281")])
    assert a["tier"] == "proxy" and a["yield"] == pytest.approx(0.055)


def test_before_excludes_the_days_own_owner_rate():
    a = R.answer(date(2026, 10, 14), 91 / 365, [own(date(2026, 10, 14), 91, "5.7281")], before=True)
    assert a["tier"] == "proxy"


def test_owner_rates_interpolate_linearly_in_yield_between_tenors_and_stay_flat_outside():
    rows = [own(date(2026, 10, 14), 91, "5.7281"), own(date(2026, 10, 14), 182, "6.2896")]
    mid = R.answer(date(2026, 10, 20), 137 / 365, rows)
    y = 0.057281 + (0.062896 - 0.057281) * (137 - 91) / (182 - 91)
    assert mid["yield"] == pytest.approx(y, abs=1e-12) and mid["rate"] == pytest.approx(z(y, 137), abs=1e-12)
    short = R.answer(
        date(2026, 10, 20), 30 / 365, rows
    )  # below the shortest node: the 91-day yield, at 30 days
    assert short["yield"] == pytest.approx(0.057281) and short["rate"] == pytest.approx(z(0.057281, 30))


def test_an_answer_from_rbis_next_scheduled_decision_on_is_labelled_possibly_stale():
    # the table was checked on 23-Oct-2026; RBI's next MPC decision was scheduled for 04-Dec-2026 (prid=62422)
    before = R.answer(date(2026, 12, 3), 91 / 365)
    assert before["stale"] is False
    on = R.answer(date(2026, 12, 4), 91 / 365)
    after = R.answer(date(2027, 1, 15), 91 / 365)
    assert on["stale"] is True and after["stale"] is True and after["yield"] == pytest.approx(0.055)
    assert "04-Dec-2026" in after["note"] and "possibly stale" in after["note"]
    # known at the start of the decision day: nothing newer can have been announced yet
    assert R.answer(date(2026, 12, 4), 91 / 365, before=True)["stale"] is False

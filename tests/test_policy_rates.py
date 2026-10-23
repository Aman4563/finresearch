"""#479: RBI's policy rates typed in by hand (fincalc.policy_rates), each row citing RBI's own release. Expected values
are the rates RBI printed, written here as literals; nothing is computed by the code under test."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import policy_rates as P


@pytest.mark.parametrize(
    ("day", "repo"),
    [
        (date(2015, 1, 1), "0.08"),  # set on 28-Jan-2014, still in force when the table starts
        (date(2015, 1, 14), "0.08"),
        (date(2015, 1, 15), "0.0775"),  # off-cycle cut, in force from the announcement day
        (date(2019, 10, 3), "0.054"),
        (date(2019, 10, 4), "0.0515"),  # the 5.15 % cut is Oct-2019, not Feb-2020
        (date(2020, 2, 6), "0.0515"),  # Feb-2020's meeting kept it unchanged
        (date(2020, 3, 27), "0.044"),
        (date(2022, 5, 4), "0.044"),  # off-cycle hike back to 4.40 %
        (date(2026, 10, 6), "0.0525"),
        (date(2026, 10, 7), "0.055"),
        (date(2026, 10, 23), "0.055"),
    ],
)
def test_repo_in_force_on_a_day(day, repo):
    assert P.in_force(day).repo == Decimal(repo)


def test_before_uses_the_rates_in_force_at_the_start_of_the_day():
    # a decision announced on the day is not used by a caller that must not see that day's news
    assert P.in_force(date(2026, 10, 7), before=True).repo == Decimal("0.0525")
    assert P.in_force(date(2026, 10, 8), before=True).repo == Decimal("0.055")


def test_before_the_tables_start_is_unknown():
    assert P.in_force(date(2014, 12, 31)) is None
    assert P.in_force(date(2014, 12, 31), before=True) is None
    assert P.in_force(date(2015, 1, 1), before=True).repo == Decimal("0.08")  # set in 2014, known at the start


def test_the_corridor_floor_names_its_instrument():
    rr, sdf = P.in_force(date(2022, 4, 7)), P.in_force(date(2022, 4, 8))
    assert (rr.floor_kind, rr.floor, rr.repo, rr.msf) == ("reverse repo", Decimal("0.0335"), Decimal("0.04"),
                                                          Decimal("0.0425"))  # fmt: skip
    assert (sdf.floor_kind, sdf.floor, sdf.repo, sdf.msf) == ("SDF", Decimal("0.0375"), Decimal("0.04"),
                                                              Decimal("0.0425"))  # fmt: skip
    now = P.in_force(date(2026, 10, 23))
    assert (now.floor_kind, now.floor, now.msf) == ("SDF", Decimal("0.0525"), Decimal("0.0575"))


def test_corridor_only_changes_are_rows_too():
    # 06-Apr-2017: repo unchanged at 6.25 %, reverse repo up to 6.00 %, MSF down to 6.50 %
    a, b = P.in_force(date(2017, 4, 5)), P.in_force(date(2017, 4, 6))
    assert a.repo == b.repo == Decimal("0.0625")
    assert (a.floor, a.msf, b.floor, b.msf) == (Decimal("0.0575"), Decimal("0.0675"), Decimal("0.06"),
                                                Decimal("0.065"))  # fmt: skip
    # 17-Apr-2020: only the reverse repo moved, 4.00 % -> 3.75 %
    c, d = P.in_force(date(2020, 4, 16)), P.in_force(date(2020, 4, 17))
    assert c.repo == d.repo == Decimal("0.044") and (c.floor, d.floor) == (Decimal("0.04"), Decimal("0.0375"))


def test_repo_change_between_two_days():
    assert P.repo_change(date(2026, 9, 30), date(2026, 10, 23)) == Decimal("0.0025")
    assert P.repo_change(date(2026, 10, 7), date(2026, 10, 23)) == Decimal("0")
    assert P.repo_change(date(2019, 1, 1), date(2020, 6, 1)) == Decimal("-0.025")  # 6.50 % -> 4.00 %
    assert P.repo_change(date(2014, 6, 1), date(2015, 6, 1)) is None  # the start date's rate is unknown


def test_every_row_cites_an_rbi_release_and_the_rows_are_ordered():
    rows = P.TABLE
    assert rows[0].effective <= P.START < rows[1].effective
    assert all(a.effective < b.effective for a, b in zip(rows, rows[1:]))
    for r in rows:
        assert r.url.startswith("https://www.rbi.org.in/"), r
        assert r.title and r.released >= r.effective
        assert Decimal(0) < r.floor < r.repo < r.msf < Decimal("0.2"), r  # fractions, not percent
    assert (rows[-1].effective, rows[-1].repo) == (date(2026, 10, 7), Decimal("0.055"))

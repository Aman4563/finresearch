"""fincalc.surprise: SUE, t0, abnormal returns, deciles and clustered errors, against hand-computed values (#181)."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from finresearch.fincalc import surprise as S

IST = S.IST
ENDS = [S.quarter_back(date(2024, 3, 31), k) for k in range(12, -1, -1)]  # Mar-2021 .. Mar-2024
# EPS for Mar21 .. Mar24; the Mar24 difference is 20 - 14 = 6 and the eight before it are 1,3,0,3,2,0,2,1
EPS = [10, 11, 12, 13, 11, 13, 12, 15, 14, 13, 15, 16, 20]
# mean 1.5, squared deviations sum to 10, s = sqrt(10/7) -> SUE = 6 / sqrt(10/7) = 5.019960...
HAND_SUE = 6 / math.sqrt(10 / 7)


def _at(end: date, days: int = 20, hour: int = 19) -> datetime:
    return datetime.combine(end + timedelta(days=days), datetime.min.time(), IST).replace(hour=hour)


def _quarters(eps=EPS, ends=ENDS) -> list[S.Quarter]:
    return [S.Quarter(e, _at(e), float(v)) for e, v in zip(ends, eps, strict=True)]


def test_quarter_back():
    assert S.quarter_back(date(2024, 3, 31), 1) == date(2023, 12, 31)
    assert S.quarter_back(date(2024, 3, 31), 4) == date(2023, 3, 31)
    assert S.quarter_back(date(2023, 12, 31), 3) == date(2023, 3, 31)
    assert S.quarter_back(date(2024, 6, 30), 2) == date(2023, 12, 31)


def test_sue_hand_computed():
    v, why = S.sue(_quarters(), date(2024, 3, 31))
    assert why is None
    assert v == pytest.approx(HAND_SUE, rel=1e-12)
    assert v == pytest.approx(5.01996, abs=1e-5)


def test_sue_needs_six_past_differences():
    qs = [q for q in _quarters() if q.end >= date(2021, 12, 31)]  # Mar21-Sep21 gone: 5 differences left
    v, why = S.sue(qs, date(2024, 3, 31))
    assert v is None and "5 of 8" in why


def test_sue_split_adjusted_is_unchanged():
    # a 1:1 bonus went ex between the Dec-23 and Mar-24 filings: Mar-24 EPS is reported on twice the shares (10, not
    # 20); every earlier EPS is halved onto that basis, so the difference and s halve and SUE is the same
    eps = [*EPS[:-1], 10]
    acts = [(date(2024, 2, 15), "Bonus 1:1")]
    v, why = S.sue(_quarters(eps), date(2024, 3, 31), acts)
    assert why is None and v == pytest.approx(HAND_SUE, rel=1e-12)
    # without the action the same figures read as a collapse
    assert S.sue(_quarters(eps), date(2024, 3, 31))[0] < 0


def test_sue_does_not_use_later_filings():
    base = S.sue(_quarters(), date(2023, 12, 31))
    later = [*_quarters(), S.Quarter(date(2024, 6, 30), _at(date(2024, 6, 30)), 99.0)]
    assert S.sue(later, date(2023, 12, 31)) == base
    # a quarter whose filing was broadcast AFTER the target's announcement is unknown at that time
    late = [S.Quarter(q.end, _at(date(2024, 3, 31), 30) if q.end == date(2023, 3, 31) else q.announced, q.value)
            for q in _quarters()]  # fmt: skip
    v, why = S.sue(late, date(2024, 3, 31))
    assert v is None and "a year earlier" in why
    # and a split that goes ex after the target's filing does not rescale it
    acts = [(date(2024, 5, 1), "Bonus 1:1")]
    assert S.sue(_quarters(), date(2024, 3, 31), acts)[0] == pytest.approx(HAND_SUE)


def test_sue_zero_variation():
    flat = [10.0 + (i // 4) for i in range(13)]  # every seasonal difference is exactly 1
    v, why = S.sue(_quarters(flat), date(2024, 3, 31))
    assert v is None and "do not vary" in why


SESSIONS = [date(2024, 10, d) for d in (14, 15, 16, 17, 18, 21, 22)]


def _ist(d: int, h: int, m: int = 0) -> datetime:
    return datetime(2024, 10, d, h, m, tzinfo=IST)


def test_event_session_after_close_moves_to_next_session():
    assert S.event_session(_ist(17, 14), True, SESSIONS) == date(2024, 10, 17)  # during market hours
    assert S.event_session(_ist(17, 8), True, SESSIONS) == date(2024, 10, 17)  # before the open
    assert S.event_session(_ist(17, 15, 30), True, SESSIONS) == date(
        2024, 10, 18
    )  # at the close: next session
    assert S.event_session(_ist(18, 19, 42), True, SESSIONS) == date(2024, 10, 21)  # Friday evening -> Monday
    assert S.event_session(_ist(19, 10), True, SESSIONS) == date(2024, 10, 21)  # Saturday -> Monday
    assert S.event_session(_ist(17, 0), False, SESSIONS) == date(
        2024, 10, 18
    )  # no time of day: after the close
    holiday = [d for d in SESSIONS if d != date(2024, 10, 18)]
    assert S.event_session(_ist(17, 19), True, holiday) == date(2024, 10, 21)
    assert S.event_session(_ist(22, 16), True, SESSIONS) is None  # beyond the calendar
    # a UTC timestamp is read in IST: 09:00 UTC is 14:30 IST, before the close
    assert S.event_session(datetime(2024, 10, 17, 9, 0, tzinfo=S.ZoneInfo("UTC")), True, SESSIONS) == date(
        2024, 10, 17
    )


def test_abnormal_return_hand_computed():
    stock = dict(zip(SESSIONS, [100, 102, 110, 99, 120, 121, 125], strict=True))
    index = dict(zip(SESSIONS, [1000, 1010, 1020, 1030, 1040, 1050, 1060], strict=True))
    t0 = date(2024, 10, 16)
    # [0,+1]: close of t0-1 (15th) to close of t0+1 (17th): 99/102 - 1030/1010
    assert S.abnormal_return(stock, index, SESSIONS, t0, 0, 1) == pytest.approx(99 / 102 - 1030 / 1010)
    # [+2,+3]: close of t0+1 (17th) to close of t0+3 (21st): 121/99 - 1050/1030
    assert S.abnormal_return(stock, index, SESSIONS, t0, 2, 3) == pytest.approx(121 / 99 - 1050 / 1030)
    assert S.abnormal_return(stock, index, SESSIONS, t0, 2, 10) is None  # runs past the calendar
    del index[date(2024, 10, 17)]
    assert S.abnormal_return(stock, index, SESSIONS, t0, 0, 1) is None  # a missing close is not filled
    assert S.abnormal_return(stock, index, SESSIONS, date(2024, 10, 19), 0, 1) is None  # t0 not a session


def test_deciles_and_decile_of():
    assert S.deciles(list(range(20))) == [1 + k // 2 for k in range(20)]
    assert S.deciles([5.0, -1.0, 3.0]) == [7, 1, 4]  # ranks 2, 0, 1 of 3 -> 1 + 10r // 3
    ref = [float(k) for k in range(100)]
    assert S.decile_of(-5.0, ref) == 1
    assert S.decile_of(55.0, ref) == 6  # 55 below -> 1 + 550 // 100
    assert S.decile_of(1e9, ref) == 10
    assert S.decile_of(1.0, []) is None


def test_clustered_mean_and_diff_hand_computed():
    # mean 3; influences (x - 3)/4 = -.5, -.25, 0, .75; cluster sums -.75, .75; se = sqrt(2 * 1.125) = 1.5
    e = S.clustered_mean([1, 2, 3, 6], ["a", "a", "b", "b"])
    assert (e.value, e.se, e.t, e.n, e.clusters) == (3, pytest.approx(1.5), pytest.approx(2.0), 4, 2)
    # a = [1, 3] in months m1, m2; b = [0, 2] in m2, m1: diff 1; sums m1 = -.5 - .5, m2 = .5 + .5; se = sqrt(2*2) = 2
    d = S.clustered_diff([1, 3], ["m1", "m2"], [0, 2], ["m2", "m1"])
    assert d.value == 1 and d.se == pytest.approx(2.0) and d.t == pytest.approx(0.5)
    assert S.clustered_mean([1.0, 2.0], ["x", "x"]).se is None  # one cluster: no error


def test_results_deadline_and_plausible_announcement():
    assert S.results_deadline(date(2023, 12, 31)) == date(2024, 2, 14)  # 45 days
    assert S.results_deadline(date(2024, 3, 31)) == date(2024, 5, 30)  # 60 days, annual results
    assert S.results_deadline(date(2020, 3, 31)) == date(2020, 7, 31)  # COVID extension
    ok = datetime(2019, 4, 12, 16, 0, tzinfo=IST)
    reupload = datetime(2019, 9, 4, 18, 34, tzinfo=IST)  # INFY's Mar-2019 rows in NSE's index (ADDENDUM 1)
    assert S.plausible_announcement(date(2019, 3, 31), ok)
    assert not S.plausible_announcement(date(2019, 3, 31), reupload)
    assert S.plausible_announcement(date(2023, 12, 31), datetime(2024, 2, 21, 20, tzinfo=IST))  # deadline + 7
    assert not S.plausible_announcement(date(2023, 12, 31), datetime(2024, 2, 22, 20, tzinfo=IST))
    assert not S.plausible_announcement(date(2023, 12, 31), datetime(2023, 12, 30, 20, tzinfo=IST))

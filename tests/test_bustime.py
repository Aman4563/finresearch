"""The business-time clock (#384): hand-computed T in session units, and unknown inputs stay unknown.

Weights in these tests: w_o = 0.25, w_w = 0.2, so Y = 252 + 252 x 0.25 + 113 x 0.2 = 252 + 63 + 22.6 = 337.6.
Fri 16-Oct-2026, Sat 17, Sun 18, Mon 19, Tue 20 (expiry)."""

from datetime import date, timedelta

import pytest

from finresearch.fincalc import bustime as bt
from finresearch.fincalc.dates import ist_datetime

W = bt.Weights(overnight=0.25, nontrading=0.2, source="test")
Y = 337.6
FRI, SAT, MON, TUE = date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 19), date(2026, 10, 20)
CAL = bt.Calendar(known_from=date(2026, 1, 1), known_through=date(2026, 12, 31), holidays=frozenset())


def t(expiry, now, cal=CAL, w=W, events=bt.EXCLUDED):
    return bt.business_years(expiry, now, cal, w, events=events)


def test_units_per_year() -> None:
    assert bt.units_per_year(W) == pytest.approx(Y)
    assert bt.units_per_year(bt.PRIOR) == pytest.approx(252 + 113 * 0.10)


def test_friday_close_to_tuesday_expiry_counts_two_sessions_two_overnights_and_the_weekend() -> None:
    r = t(TUE, ist_datetime(FRI, 15, 30))
    assert (r.sessions, r.overnights, r.nontrading, r.today) == (2, 2, 2, 0.0)
    assert r.value == pytest.approx(2.9 / Y)  # 2 + 2 x 0.25 + 2 x 0.2


def test_expiry_day_at_12_30_is_the_rest_of_the_session_only() -> None:
    r = t(TUE, ist_datetime(TUE, 12, 30))
    assert r.today == pytest.approx(0.48)  # 180 of 375 minutes
    assert r.value == pytest.approx(0.48 / Y)


def test_before_the_open_today_counts_its_overnight_gap_and_whole_session() -> None:
    assert t(TUE, ist_datetime(TUE, 8, 0)).value == pytest.approx(1.25 / Y)


def test_at_the_expiry_close_it_is_zero_and_after_it_expired() -> None:
    assert t(TUE, ist_datetime(TUE, 15, 30)).value == 0.0
    r = t(TUE, ist_datetime(TUE, 15, 31))
    assert r.value is None and "expired" in r.reason


def test_a_holiday_monday_weighs_as_a_weekend_day() -> None:
    cal = bt.Calendar(CAL.known_from, CAL.known_through, frozenset({MON}))
    r = t(TUE, ist_datetime(FRI, 15, 30), cal)
    assert (r.sessions, r.nontrading) == (1, 3)
    assert r.value == pytest.approx(1.85 / Y)  # 1.25 + 3 x 0.2


def test_on_a_saturday_noon_half_the_day_is_left() -> None:
    assert t(TUE, ist_datetime(SAT, 12, 0)).value == pytest.approx(2.8 / Y)  # 0.1 + 0.2 + 2.5


def test_one_calendar_year_of_252_sessions_and_113_nontrading_days_is_one_year() -> None:
    start = date(2027, 1, 4)  # a Monday: from its close to the close of Tue 4-Jan-2028, 365 days later
    days = [start + timedelta(days=i) for i in range(1, 366)]
    weekdays = [d for d in days if d.weekday() < 5]
    hol = frozenset(
        weekdays[: len(weekdays) - 252]
    )  # the first weekdays become holidays, leaving 252 sessions
    cal = bt.Calendar(start, days[-1], hol)
    r = t(days[-1], ist_datetime(start, 15, 30), cal)
    assert (r.sessions, r.overnights, r.nontrading) == (252, 252, 113)
    assert r.value == pytest.approx(1.0)


def test_registered_prior_friday_to_tuesday() -> None:
    r = t(TUE, ist_datetime(FRI, 15, 30), w=bt.PRIOR)
    assert r.value == pytest.approx(2.2 / 263.3)  # (2 + 2 x 0.10) / (252 + 11.3)


def test_event_variance_is_added_on_its_day() -> None:
    ev = bt.EventVariance(covered_through=date(2026, 12, 31), extra={MON: 0.5, date(2026, 10, 21): 9.0})
    assert t(TUE, ist_datetime(FRI, 15, 30), events=ev).value == pytest.approx(3.4 / Y)


def test_unknown_event_calendar_is_unknown_never_no_events() -> None:
    r = t(TUE, ist_datetime(FRI, 15, 30), events=None)
    assert r.value is None and "event calendar" in r.reason
    short = bt.EventVariance(covered_through=MON, extra={})
    r = t(TUE, ist_datetime(FRI, 15, 30), events=short)
    assert r.value is None and "event calendar" in r.reason


def test_unknown_holiday_list_is_unknown_never_no_holidays() -> None:
    cal = bt.Calendar(date(2026, 1, 1), MON, frozenset())
    r = t(TUE, ist_datetime(FRI, 15, 30), cal)
    assert r.value is None and "holiday" in r.reason
    assert t(TUE, ist_datetime(FRI, 15, 30), None).value is None


def test_expiry_on_a_non_session_is_refused() -> None:
    cal = bt.Calendar(CAL.known_from, CAL.known_through, frozenset({TUE}))
    r = t(TUE, ist_datetime(FRI, 15, 30), cal)
    assert r.value is None and "not a session" in r.reason


def test_naive_now_is_refused() -> None:
    from datetime import datetime

    with pytest.raises(ValueError):
        t(TUE, datetime(2026, 10, 16, 15, 30))


def test_nse_calendar_reads_cached_years_and_is_unknown_for_an_uncached_one(tmp_path) -> None:
    from finresearch.adapters import nse_holidays as h

    h.save_holidays({MON: "Synthetic holiday"}, "trading", tmp_path)
    cal = bt.nse_calendar(FRI, TUE, tmp_path)
    assert cal is not None and cal.holidays == frozenset({MON}) and cal.covers(FRI, TUE)
    assert bt.nse_calendar(FRI, date(2027, 1, 5), tmp_path) is None  # 2027's list is not cached


def test_from_sessions_marks_unlisted_weekdays_as_holidays() -> None:
    cal = bt.Calendar.from_sessions([FRI, TUE, date(2026, 10, 15)])
    assert cal is not None and cal.holidays == frozenset({MON})
    assert (cal.known_from, cal.known_through) == (date(2026, 10, 15), TUE)
    assert bt.Calendar.from_sessions([]) is None


def test_rescale_vol_keeps_total_variance() -> None:
    assert bt.rescale_vol(0.20, 4 / 365, 2.2 / 263.3) == pytest.approx(
        0.20 * (4 / 365 / (2.2 / 263.3)) ** 0.5
    )
    assert bt.rescale_vol(0.16, 0.01, 0.04) == pytest.approx(0.08)  # sqrt(1/4) = 0.5
    assert bt.rescale_vol(None, 0.01, 0.04) is None and bt.rescale_vol(0.2, 0.01, None) is None
    assert bt.rescale_vol(0.2, 0.0, 0.04) is None


def test_overnight_weight_is_the_ratio_of_sample_variances() -> None:
    # gaps var = (0.01^2 + 0.01^2) / 1 = 0.0002; sessions var = (0.02^2 + 0.02^2) / 1 = 0.0008
    assert bt.overnight_weight([0.01, -0.01], [0.02, -0.02]) == pytest.approx(0.25)
    assert bt.overnight_weight([0.01], [0.02, -0.02]) is None
    assert bt.overnight_weight([0.01, -0.01], [0.02, 0.02]) is None  # zero session variance


def test_nontrading_weight_is_the_slope_over_the_intercept_of_squared_returns() -> None:
    # r^2 = 1e-4 after 0 non-trading days and 1.44e-4 after 2: a = 1e-4, b = 0.22e-4 per day -> 0.22 of a c2c day
    pairs = [(0.01, 0), (-0.01, 0), (0.012, 2), (-0.012, 2)]
    assert bt.nontrading_weight(pairs) == pytest.approx(0.22)
    assert bt.nontrading_weight([(0.01, 0), (0.02, 0)]) is None  # no weekend in the sample

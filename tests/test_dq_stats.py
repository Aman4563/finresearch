"""Data-quality statistics (#368): robust z, MAD outliers and SLA-aware freshness in sessions. Expected values are
worked by hand in the comments; no database."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from finresearch.dq import stats


def test_robust_z_matches_hand_value():
    # history 10..30 step 1 (21 values): median 20, |x-20| = 0,1,1,...,10,10 -> MAD 5; z = (32-20)/(1.4826*5)
    hist = [float(x) for x in range(10, 31)]
    assert abs(stats.robust_z(32.0, hist, min_n=20) - 12 / (1.4826 * 5)) < 1e-12


def test_robust_z_unknown_on_short_history_or_zero_mad():
    assert stats.robust_z(5.0, [1.0, 2.0, 3.0], min_n=20) is None
    flat = [7.0] * 30
    assert stats.robust_z(7.0, flat) == 0.0  # equal to a constant history: no deviation
    assert stats.robust_z(8.0, flat) is None  # any move off a constant history: undefined, never "ok"


def test_z_status_never_ok_when_unknown():
    assert stats.z_status(None) == "unknown"
    assert stats.z_status(5.9) == "ok"
    assert stats.z_status(-6.1) == "fail"


def test_mad_outlier():
    # gaps median 2, MAD 1 (|g-2| = 1,0,1,1,0,1 -> sorted 0,0,1,1,1,1 -> median 1); 7 is 5 MAD away > 4
    gaps = [1.0, 2.0, 3.0, 1.0, 2.0, 3.0]
    assert stats.mad_status(7.0, gaps, k=4, min_n=5) == "fail"
    assert stats.mad_status(5.5, gaps, k=4, min_n=5) == "ok"
    assert stats.mad_status(5.5, gaps[:3], k=4, min_n=5) == "unknown"


IST_2030 = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)  # Wed 20:30 IST
IST_2000 = datetime(2026, 9, 30, 14, 30, tzinfo=UTC)  # Wed 20:00 IST


def test_expected_day_respects_sla_and_holidays():
    assert stats.expected_day(IST_2030, time(20, 30), set()) == date(2026, 9, 30)
    assert stats.expected_day(IST_2000, time(20, 30), set()) == date(2026, 9, 29)
    # Tuesday a holiday: the file due before tonight's SLA is Monday's
    assert stats.expected_day(IST_2000, time(20, 30), {date(2026, 9, 29)}) == date(2026, 9, 28)
    sat = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
    assert stats.expected_day(sat, time(20, 30), set()) == date(2026, 10, 2)


def test_lag_sessions():
    assert stats.lag_sessions(date(2026, 9, 30), date(2026, 9, 30), set()) == 0
    assert stats.lag_sessions(date(2026, 9, 25), date(2026, 9, 30), set()) == 3  # Mon, Tue, Wed missing
    assert stats.lag_sessions(date(2026, 9, 25), date(2026, 9, 30), {date(2026, 9, 29)}) == 2
    assert stats.lag_sessions(None, date(2026, 9, 30), set()) is None
    assert stats.lag_sessions(date(2026, 10, 1), date(2026, 9, 30), set()) == 0  # ahead (a special session)

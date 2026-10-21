"""Historical stress replay on today's holdings (#345): synthetic fixtures only, expected values worked by hand."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from finresearch.portfolio import stress_replay as sr

D = date
W = sr.Window("t", "Test fall", D(2020, 2, 3), D(2020, 2, 7), "synthetic")
BASE = D(2020, 1, 31)
DAYS = [D(2020, 2, 3), D(2020, 2, 4), D(2020, 2, 5), D(2020, 2, 6), D(2020, 2, 7), D(2020, 2, 10), D(2020, 2, 11)]
INDEX = dict(zip([BASE, *DAYS], [100, 98, 95, 97, 90, 92, 99, 101], strict=True))


def test_the_five_dated_windows_of_the_issue():
    got = {w.id: (w.start, w.end) for w in sr.WINDOWS}
    assert got == {
        "gfc2008": (D(2008, 9, 1), D(2008, 11, 30)),
        "taper2013": (D(2013, 5, 1), D(2013, 8, 31)),
        "covid2020": (D(2020, 2, 1), D(2020, 3, 31)),
        "election2024": (D(2024, 6, 3), D(2024, 6, 5)),
        "febmar2026": (D(2026, 2, 1), D(2026, 3, 31)),
    }


def test_market_days_start_from_the_last_close_before_the_window():
    base, days = sr.market_days(INDEX, W, D(2020, 2, 11))
    assert base == BASE and days == DAYS
    base, days = sr.market_days(INDEX, W, D(2020, 2, 7))  # the recovery horizon cuts the path
    assert days == DAYS[:5]


def test_market_days_refuse_a_gap_or_a_missing_base():
    gap = {d: v for d, v in INDEX.items() if d not in DAYS[:4]}
    gap = {**gap, D(2020, 1, 30): 99}
    gap.pop(BASE)  # 30-Jan -> 7-Feb is 8 calendar days: the backfill has a hole, never read as "no move"
    assert isinstance(sr.market_days(gap, W, D(2020, 2, 11)), str)
    late = {d: v for d, v in INDEX.items() if d != BASE}
    assert "before" in sr.market_days(late, W, D(2020, 2, 11))
    short = {d: v for d, v in INDEX.items() if d <= D(2020, 2, 5)}  # data stops inside the window
    assert isinstance(sr.market_days(short, W, D(2020, 2, 11)), str)


def test_own_path_chains_closes_and_applies_an_in_window_split():
    closes = {BASE: 100.0, DAYS[0]: 90.0, DAYS[1]: 45.0, DAYS[2]: 45.0, DAYS[3]: 40.5, DAYS[4]: 45.0}
    r, why = sr.own_returns(closes, {DAYS[1]: 2.0}, BASE, DAYS[:5], W.end)
    assert why is None
    assert r == pytest.approx({DAYS[0]: -0.10, DAYS[1]: 0.0, DAYS[2]: 0.0, DAYS[3]: -0.10, DAYS[4]: 45 / 40.5 - 1})


def test_own_path_rejects_an_unrecorded_split_instead_of_a_fake_crash():
    closes = {BASE: 100.0, DAYS[0]: 100.0, DAYS[1]: 50.0, DAYS[2]: 50.0, DAYS[3]: 50.0, DAYS[4]: 50.0}
    r, why = sr.own_returns(closes, {}, BASE, DAYS[:5], W.end)
    assert r is None and "split" in why


def test_own_path_needs_a_price_at_the_start_and_through_the_window():
    closes = {d: 10.0 for d in DAYS}
    r, why = sr.own_returns(closes, {}, BASE, DAYS, W.end)  # listed after the window began
    assert r is None and "start" in why
    thin = {BASE: 10.0, DAYS[4]: 9.0}
    r, why = sr.own_returns(thin, {}, BASE, DAYS[:5], W.end)
    assert r is None and "window" in why


def test_own_path_stops_after_the_window_where_prices_end():
    closes = {BASE: 10.0, **{d: 10.0 for d in DAYS[:5]}}
    r, why = sr.own_returns(closes, {}, BASE, DAYS, W.end)
    assert why is None and sorted(r) == DAYS[:5]  # 10/11-Feb have no close: the path ends, never ffilled

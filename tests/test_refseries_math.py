"""Reference-series maths (#366): approximate total-return index, par -> zero bootstrap, the short-rate proxy, the
dividend-yield forward and the VIX vs IV30 comparison. Expected values are worked by hand in the comments."""

from __future__ import annotations

import math
from datetime import date

import pytest

from finresearch.fincalc import refseries as rs


def test_approx_tri_adds_a_day_of_dividend_yield():
    # TR_1 = 100 x (102/100 + 0.0252/252) = 100 x (1.02 + 0.0001) = 102.01
    out = rs.approx_tri([(date(2024, 1, 1), 100.0, 2.52), (date(2024, 1, 2), 102.0, 2.52)])
    assert out[0] == (date(2024, 1, 1), 100.0)
    assert out[1][1] == pytest.approx(102.01, abs=1e-9)


def test_approx_tri_stops_at_an_unknown_yield():
    # an unknown yield (or close) is never read as 0: the chain cannot continue past it
    out = rs.approx_tri([(date(2024, 1, 1), 100.0, 1.0), (date(2024, 1, 2), 101.0, None),
                         (date(2024, 1, 3), 102.0, 1.0)])  # fmt: skip
    assert out[1][1] is None and out[2][1] is None


def test_flat_par_curve_bootstraps_to_a_flat_zero_curve():
    # flat 6 % semi-annual par: every discount factor is 1.03^-2T, so r = 2 ln 1.03 = 5.91176 % continuous
    zeros = rs.zero_curve([(0.5, 0.06), (1.0, 0.06), (1.5, 0.06), (2.0, 0.06)])
    for t, r in zeros:
        assert r == pytest.approx(2 * math.log(1.03), abs=1e-12), t


def test_rising_par_curve_bootstrap():
    # P(0.5) = 1/1.03 = 0.9708737864; P(1) = (1 - 0.035 x 0.9708737864) / 1.035 = 0.9333521
    # r(1) = -ln 0.9333521 = 0.0689728
    zeros = dict(rs.zero_curve([(0.5, 0.06), (1.0, 0.07)]))
    assert zeros[1.0] == pytest.approx(-math.log((1 - 0.035 / 1.03) / 1.035), abs=1e-12)
    assert zeros[1.0] == pytest.approx(0.0689728, abs=1e-7)


def test_bootstrap_uses_the_half_year_grid_and_refuses_a_gap():
    zeros = dict(rs.zero_curve([(0.25, 0.055), (0.5, 0.06), (0.75, 0.065), (1.0, 0.07)]))
    assert set(zeros) == {0.5, 1.0}  # quarter-year tenors are not coupon dates of a semi-annual bond
    with pytest.raises(ValueError, match=r"1\.0 year"):
        rs.zero_curve([(0.5, 0.06), (1.5, 0.07)])


def test_short_rate_proxy():
    assert rs.short_rate_proxy(0.065) == pytest.approx(2 * math.log(1.0325), abs=1e-15)


def test_forward_with_dividend_yield():
    # F = 100 x e^((0.07 - 0.015) x 0.5)
    assert rs.forward(100.0, 0.07, 0.015, 0.5) == pytest.approx(100 * math.exp(0.0275), abs=1e-12)


def test_vix_vs_iv30_reports_change_correlation_and_level_gap():
    vix = [15.0, 16.0, 15.5, 17.0, 16.0]
    iv30 = [14.0, 15.0, 14.5, 16.0, 15.0]  # always 1 vol point below: changes identical
    res = rs.vix_vs_iv30(vix, iv30)
    assert res["n"] == 5 and res["change_correlation"] == pytest.approx(1.0)
    assert res["median_gap"] == pytest.approx(1.0) and res["mad_gap"] == pytest.approx(0.0)
    assert rs.vix_vs_iv30([15.0, None], [14.0, 13.0])["n"] == 1  # unknown pairs dropped, never zero

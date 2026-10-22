"""#383 post-trade maths (fincalc.posttrade): implementation shortfall, the daily P&L explain by greeks and by full
revaluation, the residual acceptance test, plan adherence and the exposure mix. Expected values are worked by hand in
the comments (synthetic trades only)."""

from __future__ import annotations

import math

import pytest

from finresearch.fincalc import posttrade as pt


# --------------------------------------------------------------------------- implementation shortfall
def test_shortfall_hand_example():
    # sell 50 at mid 100 filled 99.50: -1 x (99.50 - 100) x 50 = +25 (received 25 less than the mid)
    # buy 50 at mid 40 filled 40.30:   +1 x (40.30 - 40) x 50 = +15 (paid 15 more)
    # fees 40 -> IS = 25 + 15 + 40 = 80; base = |net premium at the mids| = |100x50 - 40x50| = 3000 -> 2.6667 %
    got = pt.shortfall([pt.FillLeg("sell", 50, 99.5, 100.0), pt.FillLeg("buy", 50, 40.3, 40.0)], fees=40.0)
    assert got.price == pytest.approx(40.0)
    assert got.fees == 40.0
    assert got.total == pytest.approx(80.0)
    assert got.base == pytest.approx(3000.0)
    assert got.pct == pytest.approx(80 / 3000 * 100)
    assert got.why is None


def test_shortfall_better_fill_is_negative():
    # buy 25 at mid 10 filled 9.80: +1 x (9.80 - 10) x 25 = -5; no fees -> -5 (a price improvement)
    got = pt.shortfall([pt.FillLeg("buy", 25, 9.8, 10.0)], fees=0.0)
    assert got.total == pytest.approx(-5.0)
    assert got.pct == pytest.approx(-5 / 250 * 100)


def test_shortfall_unknown_mid_is_unknown_never_zero():
    got = pt.shortfall([pt.FillLeg("sell", 50, 99.5, 100.0), pt.FillLeg("buy", 50, 40.3, None)], fees=40.0)
    assert got.total is None and got.price is None and got.pct is None
    assert "mid" in got.why


def test_shortfall_unknown_fees_is_unknown():
    got = pt.shortfall([pt.FillLeg("buy", 25, 9.8, 10.0)], fees=None)
    assert got.total is None and got.price == pytest.approx(-5.0)


def test_shortfall_zero_base_has_no_percent():
    # a calendar at the same mid both sides: net premium 0, the % is undefined (None), the rupees still known
    got = pt.shortfall([pt.FillLeg("sell", 10, 4.9, 5.0), pt.FillLeg("buy", 10, 5.1, 5.0)], fees=0.0)
    assert got.total == pytest.approx(2.0) and got.pct is None


# --------------------------------------------------------------------------- the greek explain
def test_greek_explain_hand_example():
    # position greeks: delta 25, gamma 0.5, vega 10 per vol point, theta -4 per calendar day
    # dS = +10, dsigma = -1 point, dt = 1 day: 250 + 0.5 x 0.5 x 100 + (-10) + (-4) = 250 + 25 - 10 - 4 = 261
    # actual 255 -> residual -6, U = 6 / 255
    e = pt.greek_explain(pt.PositionGreeks(25, 0.5, 10, -4), d_spot=10, d_vol_pts=-1, d_days=1, actual=255)
    assert (e.delta, e.gamma, e.vega, e.theta) == pytest.approx((250, 25, -10, -4))
    assert e.explained == pytest.approx(261)
    assert e.residual == pytest.approx(-6)
    assert e.unexplained == pytest.approx(6 / 255)


def test_greek_explain_zero_actual_has_no_ratio():
    e = pt.greek_explain(pt.PositionGreeks(1, 0, 0, 0), d_spot=0, d_vol_pts=0, d_days=0, actual=0)
    assert e.residual == 0 and e.unexplained is None


# --------------------------------------------------------------------------- the acceptance test (T1)
def test_tolerance_ratio_passes():
    # |residual| 6 <= 10 % of 255 = 25.5
    assert pt.within_tolerance(residual=-6, actual=255, max_loss=None) is True


def test_tolerance_near_zero_day_passes_on_the_max_loss_floor():
    # actual 2, residual 5: 10 % of 2 = 0.2 fails, but 0.5 % of a 2,000 max loss = 10 >= 5 passes
    assert pt.within_tolerance(residual=5, actual=2, max_loss=-2000) is True
    # max loss 500: floor 2.5 < 5 -> fails
    assert pt.within_tolerance(residual=5, actual=2, max_loss=-500) is False


def test_tolerance_unknown_floor_is_unknown_not_a_fail():
    # the ratio fails and the max loss is unbounded/unknown: the floor cannot be read -> None
    assert pt.within_tolerance(residual=5, actual=2, max_loss=None) is None
    assert pt.within_tolerance(residual=None, actual=2, max_loss=-2000) is None

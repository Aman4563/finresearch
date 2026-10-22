"""#383 post-trade maths (fincalc.posttrade): implementation shortfall, the daily P&L explain by greeks and by full
revaluation, the residual acceptance test, plan adherence and the exposure mix. Expected values are worked by hand in
the comments (synthetic trades only)."""

from __future__ import annotations

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


# --------------------------------------------------------------------------- full revaluation and position greeks
def test_revaluation_future_is_all_spot():
    # short 75 futures, spot 100 -> 102: spot effect -75 x 2 = -150, no vol or time effect; actual -150 -> residual 0
    legs = [pt.MarkLeg("future", 0.0, -75, None, None)]
    r = pt.revaluation(legs, s0=100, s1=102, t0=10 / 365, t1=9 / 365, rate=0.0, actual=-150)
    assert (r.spot, r.vol, r.time) == pytest.approx((-150, 0, 0))
    assert r.residual == pytest.approx(0)


def test_revaluation_deep_itm_call_tracks_intrinsic():
    # K 50, S 100 -> 110, vol 1 %, r 0: the call is worth S - K (50, then 60) to many decimals, so 50 units gain
    # 10 x 50 = 500 from spot; vol 1 % -> 2 % and one day less change nothing measurable
    legs = [pt.MarkLeg("call", 50.0, 50, 0.01, 0.02)]
    r = pt.revaluation(legs, s0=100, s1=110, t0=30 / 365, t1=29 / 365, rate=0.0, actual=500)
    assert r.spot == pytest.approx(500, abs=1e-6)
    assert r.vol == pytest.approx(0, abs=1e-6) and r.time == pytest.approx(0, abs=1e-6)
    assert r.model == pytest.approx(500, abs=1e-6) and r.residual == pytest.approx(0, abs=1e-6)


def test_revaluation_on_expiry_day_values_at_intrinsic():
    # long 50 calls K 100 at S0 100, vol 20 %, one day left; on expiry (T1 = 0) S1 = 105: worth 5 x 50 = 250.
    # spot -> vol (unknown at expiry: held at vol0, so 0) -> time: model total = 250 - V0
    from finresearch.fincalc.options import bs_price

    v0 = 50 * bs_price("call", 100, 100, 1 / 365, 0.0, 0.2)
    legs = [pt.MarkLeg("call", 100.0, 50, 0.2, None)]
    r = pt.revaluation(legs, s0=100, s1=105, t0=1 / 365, t1=0.0, rate=0.0, actual=250 - v0)
    assert r.vol == 0
    assert r.spot + r.time == pytest.approx(250 - v0)
    assert r.residual == pytest.approx(0)


def test_revaluation_unknown_iv_is_unknown():
    legs = [pt.MarkLeg("put", 100.0, -50, None, 0.2)]
    assert pt.revaluation(legs, s0=100, s1=101, t0=0.1, t1=0.09, rate=0.0, actual=10) is None
    legs = [pt.MarkLeg("put", 100.0, -50, 0.2, None)]  # T1 > 0 needs IV1
    assert pt.revaluation(legs, s0=100, s1=101, t0=0.1, t1=0.09, rate=0.0, actual=10) is None


def test_position_greeks_sum_signed_units():
    # short 75 futures: delta -75, nothing else; plus 50 deep-ITM calls at r 0 (delta ~ 1 each): delta ~ -25
    g = pt.position_greeks([pt.MarkLeg("future", 0.0, -75, None, None), pt.MarkLeg("call", 50.0, 50, 0.01, None)],
                           spot=100, t=30 / 365, rate=0.0)  # fmt: skip
    assert g.delta == pytest.approx(-25, abs=1e-6)
    assert g.gamma == pytest.approx(0, abs=1e-6) and g.vega == pytest.approx(0, abs=1e-6)
    assert pt.position_greeks([pt.MarkLeg("call", 50.0, 50, None, None)], spot=100, t=0.1, rate=0.0) is None
    assert pt.position_greeks([pt.MarkLeg("call", 50.0, 50, 0.2, None)], spot=100, t=0.0, rate=0.0) is None


# --------------------------------------------------------------------------- plan adherence
def _at(day, hh=10, mm=0):
    from datetime import datetime

    from finresearch.fincalc.dates import IST

    return datetime(2026, 9, day, hh, mm, tzinfo=IST)


def _five():
    R = pt.RuleState
    return [
        # 1: target rule fired 10:00, squared off 10:05 -> planned target
        pt.ClosedTrade(
            "square_off", _at(1, 10, 5), 3000, 10000, [R("profit", 30, _at(1)), R("stop", 30, None)]
        ),
        # 2: stop 30 % fired on the 1st, squared off on the 3rd (beyond the 1-day window) -> late;
        #    gross -4,000 on a 10,000 premium: the stop allowed 3,000 -> 1,000 beyond the stop
        pt.ClosedTrade("square_off", _at(3), -4000, 10000, [R("stop", 30, _at(1))]),
        # 3: rules armed, none fired, squared off -> discretionary
        pt.ClosedTrade("square_off", _at(4), -200, 5000, [R("profit", 50, None)]),
        # 4: stop 50 % never hit, settled at expiry -> planned (time); gross +500, nothing beyond the stop
        pt.ClosedTrade("expiry", _at(5, 15, 30), 500, 8000, [R("stop", 50, None)]),
        # 5: no exit rules at all -> no plan (left out of the rate, never counted as discretionary)
        pt.ClosedTrade("square_off", _at(6), 100, 2000, []),
    ]


def test_classify_each_exit():
    assert [pt.classify(t) for t in _five()] == [
        "planned_target",
        "late",
        "discretionary",
        "planned_time",
        "no_plan",
    ]


def test_adherence_golden_values():
    a = pt.adherence(_five())
    # planned 2 (trades 1, 4) of 4 with a plan (planned 2 + late 1 + discretionary 1) = 0.5
    assert a.counts == {"planned_target": 1, "late": 1, "discretionary": 1, "planned_time": 1, "no_plan": 1}
    assert a.rate == pytest.approx(0.5)
    # trades with a stop rule: 1, 2 and 4; breached: trade 2 only, 1,000 beyond
    assert a.with_stop == 3 and a.beyond_stop_count == 1
    assert a.mean_beyond_stop == pytest.approx(1000)


def test_adherence_without_plans_is_unknown():
    a = pt.adherence([pt.ClosedTrade("square_off", _at(6), 100, 2000, [])])
    assert a.rate is None and a.mean_beyond_stop is None


def test_beyond_stop_unknown_premium_is_unknown():
    t = pt.ClosedTrade("square_off", _at(3), -4000, None, [pt.RuleState("stop", 30, _at(1))])
    assert pt.beyond_stop(t) is None


def test_hold_to_expiry_plan_counts_an_expiry_as_planned():
    t = pt.ClosedTrade("expiry", _at(5, 15, 30), 500, 8000, [], hold_to_expiry=True)
    assert pt.classify(t) == "planned_time"


# --------------------------------------------------------------------------- exposure mix
def test_exposure_mix_golden_values():
    from datetime import date

    a = pt.MixTrade(date(2026, 9, 1), date(2026, 9, 1), [("sell", 100.0, 50), ("buy", 40.0, 50)], 0)
    b = pt.MixTrade(date(2026, 9, 1), date(2026, 9, 6), [("buy", 80.0, 25)], 2)
    m = pt.exposure_mix([a, b])
    # sold 5,000 of 5,000 + 2,000 + 2,000 = 9,000 -> 0.5556; same-day expiry 1 of 2
    assert m.premium_sold_share == pytest.approx(5000 / 9000)
    assert m.same_day_expiry_share == pytest.approx(0.5)
    # held 2 of 5 days to expiry -> 0.4; trade a (0 days to expiry) has no ratio
    assert m.mean_hold_share == pytest.approx(0.4)
    assert pt.exposure_mix([]).same_day_expiry_share is None


def test_legs_greek_explain_uses_each_legs_own_vol_change():
    from finresearch.fincalc import options as o

    # long 50 calls K 100 (IV 20 % -> 22 %) and short 50 calls K 110 (IV 18 % -> 17 %), S 100 -> 101, 30 -> 29 days
    legs = [pt.MarkLeg("call", 100.0, 50, 0.20, 0.22), pt.MarkLeg("call", 110.0, -50, 0.18, 0.17)]
    e = pt.legs_greek_explain(legs, s0=100, s1=101, t0=30 / 365, t1=29 / 365, d_days=1, rate=0.0, actual=40)
    a, b = o.greeks("call", 100, 100, 30 / 365, 0.0, 0.20), o.greeks("call", 100, 110, 30 / 365, 0.0, 0.18)
    assert e.delta == pytest.approx(50 * a.delta - 50 * b.delta)
    assert e.gamma == pytest.approx(0.5 * (50 * a.gamma - 50 * b.gamma))
    assert e.vega == pytest.approx(50 * a.vega * 2 - 50 * b.vega * -1)  # +2 points long, -1 point short
    assert e.theta == pytest.approx(50 * a.theta - 50 * b.theta)
    assert e.residual == pytest.approx(40 - e.explained)
    # on expiry day the IV is unknown and irrelevant: no vol term; an unknown IV0 leaves it unknown
    e = pt.legs_greek_explain(legs[:1], s0=100, s1=101, t0=1 / 365, t1=0.0, d_days=1, rate=0.0, actual=40)
    assert e.vega == 0
    assert pt.legs_greek_explain([pt.MarkLeg("call", 100.0, 50, None, 0.2)], s0=100, s1=101, t0=0.1, t1=0.09,
                                 d_days=1, rate=0.0, actual=40) is None  # fmt: skip

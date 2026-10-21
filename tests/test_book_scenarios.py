"""Book-level exposures and the full-revaluation scenario engine (#394, fincalc.book_scenarios). Synthetic legs only.
Expected values are computed independently in the tests (statistics.NormalDist, hand arithmetic), never by calling
the code under test."""

from __future__ import annotations

import math
from datetime import date, datetime
from statistics import NormalDist

import pytest

from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc.dates import IST

N = NormalDist()
NOW = datetime(2026, 10, 22, 15, 30, tzinfo=IST)  # a Thursday, at the close: T = calendar days / 365
EXP = date(2026, 11, 26)  # 35 days later
T35 = 35 / 365


def _b76(right: str, f: float, k: float, t: float, r: float, v: float) -> float:
    d1 = (math.log(f / k) + v * v * t / 2) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    c = math.exp(-r * t) * (f * N.cdf(d1) - k * N.cdf(d2))
    return c if right == "call" else c - math.exp(-r * t) * (f - k)


def test_black76_matches_hand_values():
    # F = K = 100, T = 1, r = 0, sigma = 20 %: 100 x (2 N(0.1) - 1) = 7.9656
    assert bs.b76_price("call", 100, 100, 1.0, 0.0, 0.2) == pytest.approx(7.965567, abs=1e-5)
    assert bs.b76_price("call", 100, 100, 1.0, 0.05, 0.2) == pytest.approx(
        7.965567 * math.exp(-0.05), abs=1e-5
    )
    assert bs.b76_price("put", 24000, 23500, T35, 0.065, 0.14) == pytest.approx(
        _b76("put", 24000, 23500, T35, 0.065, 0.14), rel=1e-9
    )
    iv = bs.b76_iv("call", _b76("call", 24000, 24500, T35, 0.065, 0.13), 24000, 24500, T35, 0.065)
    assert iv == pytest.approx(0.13, abs=1e-6)


def test_parity_forward():
    # C - P = e^{-rT}(F - K)  =>  F = K + e^{rT}(C - P)
    c, p = _b76("call", 24100, 24000, T35, 0.065, 0.14), _b76("put", 24100, 24000, T35, 0.065, 0.14)
    assert bs.parity_forward(24000, c, p, T35, 0.065) == pytest.approx(24100, abs=1e-6)


def _mkt(**kw):
    base = {"now": NOW, "rate": 0.065, "forwards": {("NIFTY", EXP): 24000.0}, "betas": {}, "nifty": 23900.0}
    return bs.Market(**{**base, **kw})


def test_leg_iv_is_inverted_from_the_mark_on_the_forward():
    mark = _b76("put", 24000, 23500, T35, 0.065, 0.15)
    st = bs.price_legs([bs.BookLeg("NIFTY", EXP, "put", 23500, -75, mark, "mid")], _mkt())[0]
    assert st.reason is None and st.t == pytest.approx(T35)
    assert st.iv == pytest.approx(0.15, abs=1e-6)
    assert st.value == pytest.approx(mark, abs=1e-6)


@pytest.mark.parametrize(
    ("leg", "mkt", "why"),
    [
        (bs.BookLeg("NIFTY", EXP, "put", 23500, -75, None, None), {}, "price"),
        (bs.BookLeg("NIFTY", EXP, "put", 23500, -75, 30000.0, "mid"), {}, "no-arbitrage"),
        (bs.BookLeg("NIFTY", EXP, "put", 23500, -75, 300.0, "mid"), {"forwards": {}}, "forward"),
        (bs.BookLeg("NIFTY", date(2026, 10, 22), "put", 23500, -75, 3.0, "mid"), {}, "expired"),
        (bs.BookLeg("NIFTY", EXP, "future", 0, 75, None, None), {"forwards": {}}, "forward"),
    ],
)
def test_unknown_inputs_leave_the_leg_unpriced_with_a_reason(leg, mkt, why):
    st = bs.price_legs([leg], _mkt(**mkt))[0]
    assert st.iv is None and st.value is None
    assert why in st.reason


def test_ltp_mark_is_flagged():
    mark = _b76("call", 24000, 24500, T35, 0.065, 0.13)
    st = bs.price_legs([bs.BookLeg("NIFTY", EXP, "call", 24500, 75, mark, "ltp")], _mkt())[0]
    assert st.reason is None and any("LTP" in f for f in st.flags)


# --------------------------------------------------------------------------- exposures
def _greeks76(right: str, f: float, k: float, t: float, r: float, v: float) -> dict[str, float]:
    d1 = (math.log(f / k) + v * v * t / 2) / (v * math.sqrt(t))
    df, phi = math.exp(-r * t), N.pdf(d1)
    delta = df * N.cdf(d1) if right == "call" else -df * N.cdf(-d1)
    gamma = df * phi / (f * v * math.sqrt(t))
    vega = f * df * phi * math.sqrt(t) / 100
    theta = (-f * df * phi * v / (2 * math.sqrt(t)) + r * _b76(right, f, k, t, r, v)) / 365
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def test_exposures_per_leg_and_per_bucket():
    put = _b76("put", 24000, 23500, T35, 0.065, 0.15)
    near = date(2026, 10, 27)  # 5 days: this week
    legs = [
        bs.BookLeg("NIFTY", EXP, "put", 23500, -75, put, "mid"),
        bs.BookLeg("NIFTY", EXP, "future", 0, 75, 24000.0, "mid"),
        bs.BookLeg("NIFTY", near, "future", 0, -150, 23950.0, "mid"),
    ]
    ex = bs.exposures(legs, _mkt(forwards={("NIFTY", EXP): 24000.0, ("NIFTY", near): 23950.0}))
    g = _greeks76("put", 24000, 23500, T35, 0.065, 0.15)
    later = ex.row("NIFTY", "later")
    assert later.exposure.rs_delta == pytest.approx(g["delta"] * 24000 * -75 + 24000 * 75, rel=1e-6)
    assert later.exposure.rs_gamma_1pct == pytest.approx(0.5 * g["gamma"] * 24000**2 * 1e-4 * -75, rel=1e-6)
    assert later.exposure.vega_pt == pytest.approx(g["vega"] * -75, rel=1e-6)
    assert later.exposure.theta_day == pytest.approx(g["theta"] * -75, rel=1e-6)
    assert ex.row("NIFTY", "this_week").exposure.rs_delta == pytest.approx(23950 * -150)
    assert ex.complete and ex.reasons == ()
    total = later.exposure.rs_delta + 23950 * -150
    assert ex.total.rs_delta == pytest.approx(total)
    assert ex.nifty_delta == pytest.approx(total / 23900)  # NIFTY beta = 1, in NIFTY units


def test_buckets():
    assert bs.bucket(date(2026, 10, 29), date(2026, 10, 22)) == "this_week"  # 7 days
    assert bs.bucket(date(2026, 10, 30), date(2026, 10, 22)) == "this_month"
    assert (
        bs.bucket(date(2026, 11, 3), date(2026, 10, 28)) == "this_week"
    )  # across a month end: the week wins
    assert bs.bucket(date(2026, 11, 24), date(2026, 10, 22)) == "later"


def test_an_unpriced_leg_makes_its_row_and_the_total_unknown_never_zero():
    legs = [
        bs.BookLeg("NIFTY", EXP, "future", 0, 75, 24000.0, "mid"),
        bs.BookLeg("NIFTY", EXP, "put", 23500, -75, None, None),
        bs.BookLeg("BANKNIFTY", EXP, "future", 0, 35, 52000.0, "mid"),
    ]
    ex = bs.exposures(legs, _mkt(forwards={("NIFTY", EXP): 24000.0, ("BANKNIFTY", EXP): 52000.0}))
    assert not ex.complete and ex.total is None and ex.nifty_delta is None
    assert ex.row("NIFTY", "later").exposure is None
    assert ex.row("BANKNIFTY", "later").exposure.rs_delta == pytest.approx(52000 * 35)
    assert any("23500" in r for r in ex.reasons)


def test_nifty_equivalent_delta_needs_every_beta():
    legs = [bs.BookLeg("BANKNIFTY", EXP, "future", 0, 35, 52000.0, "mid")]
    fw = {("BANKNIFTY", EXP): 52000.0}
    ex = bs.exposures(legs, _mkt(forwards=fw))
    assert ex.complete and ex.total.rs_delta == pytest.approx(52000 * 35)
    assert ex.nifty_delta is None and any("beta" in r for r in ex.nifty_delta_reasons)
    ex = bs.exposures(legs, _mkt(forwards=fw, betas={"BANKNIFTY": 1.2}))
    assert ex.nifty_delta == pytest.approx(1.2 * 52000 * 35 / 23900)

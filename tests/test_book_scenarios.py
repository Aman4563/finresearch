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

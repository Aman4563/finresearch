"""Black-76 IVs, forwards, 30-day interpolation and realised volatility from bhavcopy rows (#306). Expected values
are computed independently (closed forms by hand), not by the code under test."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from finresearch.fincalc import fno_history as fh
from finresearch.fincalc.volatility import iv_stats


def test_black76_known_values():
    # F = K = 100, T = 1, σ = 20 %: C = e^(−rT)·F·(2N(0.1) − 1); 2N(0.1) − 1 = 0.0796557 (normal table)
    assert fh.black76_price("call", 100, 100, 1.0, 0.0, 0.2) == pytest.approx(7.96557, abs=1e-4)
    assert fh.black76_price("call", 100, 100, 1.0, 0.05, 0.2) == pytest.approx(
        7.96557 * math.exp(-0.05), abs=1e-4
    )
    assert fh.black76_price("put", 100, 100, 1.0, 0.05, 0.2) == pytest.approx(7.57708, abs=1e-4)  # ATM: C = P
    assert fh.black76_iv("call", 7.57708, 100, 100, 1.0, 0.05) == pytest.approx(0.2, abs=1e-5)
    assert fh.black76_iv("call", 0.0, 100, 100, 1.0, 0.05) is None


def test_parity_forward_hand_computed():
    q = [fh.OptQuote(100, "call", 6.0), fh.OptQuote(100, "put", 4.0), fh.OptQuote(110, "call", 2.0),
         fh.OptQuote(110, "put", 11.5)]  # fmt: skip
    # |C − P| is 2 at 100 and 9.5 at 110: F = 100 + e^(0.05·0.5)·2 = 102.0506
    assert fh.parity_forward(q, 0.5, 0.05) == pytest.approx(100 + math.exp(0.025) * 2, abs=1e-9)
    assert fh.parity_forward([fh.OptQuote(100, "call", 6.0)], 0.5, 0.05) is None


def _chain(fwd: float, t: float, r: float, vol_of_k, strikes) -> list[fh.OptQuote]:
    return [fh.OptQuote(k, right, fh.black76_price(right, fwd, k, t, r, vol_of_k(k)))
            for k in strikes for right in ("call", "put")]  # fmt: skip


def test_expiry_iv_recovers_flat_vol_from_future_and_from_parity():
    day, exp = date(2026, 9, 30), date(2026, 10, 30)  # 30 days
    t = 30 / 365
    q = _chain(22700.0, t, fh.RATE, lambda k: 0.15, range(22000, 23500, 100))
    by_future = fh.expiry_iv(day, exp, q, future=22700.0)
    assert by_future.forward_source == "future" and by_future.atm_strike == 22700
    assert by_future.atm_iv == pytest.approx(15.0, abs=1e-3) and by_future.skew_25d == pytest.approx(
        0.0, abs=1e-3
    )
    by_parity = fh.expiry_iv(day, exp, q)  # no future: put-call parity gives the same forward
    assert by_parity.forward_source == "parity" and by_parity.forward == pytest.approx(22700.0, abs=1e-6)
    assert fh.expiry_iv(day, day, q, future=22700.0) is None  # expiry day: T = 0 is never used


def test_skew_sign_with_a_put_skew():
    day, exp = date(2026, 9, 30), date(2026, 10, 30)
    q = _chain(100.0, 30 / 365, fh.RATE, lambda k: 0.20 + (100 - k) * 0.004, range(80, 121, 2))
    e = fh.expiry_iv(day, exp, q, future=100.0)
    assert e.atm_iv == pytest.approx(20.0, abs=1e-3) and e.skew_25d > 0  # puts dearer than calls


def test_constant_maturity_total_variance():
    # 20 % at 10 days, 25 % at 40 days: σ²(30)·30 = 0.04·10 + (0.0625·40 − 0.04·10)·(20/30) = 1.8 → σ = √0.06 = 24.4949 %
    want = math.sqrt((0.04 * 10 + (0.0625 * 40 - 0.04 * 10) * 20 / 30) / 30) * 100
    iv, how = fh.constant_maturity([(10, 20.0), (40, 25.0)])
    assert (iv, how) == (pytest.approx(want, abs=1e-9), "interpolated") and want == pytest.approx(
        24.4949, abs=1e-4
    )
    assert fh.constant_maturity([(35, 18.0), (63, 19.0)]) == (18.0, "flat")
    assert fh.constant_maturity([(3, 18.0), (10, 19.0)]) is None
    assert fh.constant_maturity([(2, 30.0), (29, 15.9)]) == (15.9, "flat")  # longest expiry 1 day short of 30
    assert fh.constant_maturity([(2, 30.0), (22, 15.9)]) is None  # 8 days short: no stand-in


def test_front_future_returns_stay_within_one_contract():
    d0, d1, d2 = date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)
    e1, e2 = date(2026, 10, 27), date(2026, 11, 24)
    futs = [(d0, e1, 100.0), (d0, e2, 101.0), (d1, e1, 102.0), (d1, e2, 103.02), (d2, e2, 104.0)]
    r = fh.front_future_returns(futs)
    # d1: e1 (expiry after d0, on both days) 100 -> 102; d2: e1 has expired, e2 103.02 -> 104
    assert r == [(d1, pytest.approx(math.log(1.02))), (d2, pytest.approx(math.log(104 / 103.02)))]


def test_realised_vol_needs_a_full_window():
    rets = [0.01, -0.01] * 10
    # mean 0, sample variance = 20·0.0001/19, annualised ×252
    assert fh.realised_vol_pct(rets) == pytest.approx(math.sqrt(20 * 1e-4 / 19 * 252) * 100, abs=1e-9)
    assert fh.realised_vol_pct(rets[:19]) is None


def test_day_summary_term_structure_and_30d():
    day = date(2026, 9, 30)
    exps = {day + timedelta(days=d): v for d, v in ((6, 0.30), (13, 0.16), (27, 0.17), (55, 0.18))}
    opts = {
        e: _chain(100.0, (e - day).days / 365, fh.RATE, lambda k, v=v: v, range(90, 111, 5))
        for e, v in exps.items()
    }
    s = fh.day_summary(day, opts, {day + timedelta(days=27): 100.0}, spot=None)
    assert s.near.dte == 13 and s.next.dte == 27  # the 6-day expiry is below MIN_DTE
    assert s.near.forward_source == "parity" and s.next.forward_source == "future"
    want = math.sqrt((0.17**2 * 27 + (0.18**2 * 55 - 0.17**2 * 27) * 3 / 28) / 30) * 100
    assert s.iv_30d == pytest.approx(want, abs=1e-3) and s.iv_30d_method == "interpolated"


def test_iv_rank_and_percentile_math():
    series = [10.0 + i * 0.1 for i in range(100)] + [12.0]  # min 10, max 19.9, today 12
    st = iv_stats(series)
    assert st.rank == pytest.approx((12 - 10) / (19.9 - 10)) and st.status == "ok"
    assert st.percentile == pytest.approx(20 / 100)  # 10.0 .. 11.9: 20 of the 100 earlier days are below 12


def test_illiquid_expiries_are_left_out():
    day, exp = date(2024, 7, 5), date(2024, 8, 8)
    t = 34 / 365
    one_call = [fh.OptQuote(24350, "call", fh.black76_price("call", 24333.1, 24350, t, fh.RATE, 0.197))]
    assert (
        fh.expiry_iv(day, exp, one_call, spot=24300.0) is None
    )  # 5-Jul-2024: a lone call is not an ATM level
    far = _chain(24400.0, t, fh.RATE, lambda k: 0.12, [23000])  # the only two-sided strike is 5.7 % away
    assert fh.expiry_iv(day, exp, far, future=24400.0) is None


def test_market_settle_screens_model_prices():
    assert fh.market_settle(316.25, 316.25, 47709)
    assert not fh.market_settle(
        442.05, 355.00, 89
    )  # NIFTY 8-Aug-2024 24500 CE on 10-Jul-2024: a model settle
    assert not fh.market_settle(3248.5, 1897.1, 0) and not fh.market_settle(100.0, 100.0, 9)

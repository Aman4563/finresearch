"""What a fitted SVI smile gives (#386, P3.3): the risk-neutral density and probabilities, 25-delta risk reversal and
butterfly, constant-maturity interpolation, smile-consistent greeks and Bakshi-Kapadia-Madan moments. Each value is
checked on an independent path: closed-form lognormal values on a flat smile, finite differences of the Black-76
engine (fincalc/black76.py) on a skewed one, and fincalc.options.outcome for the P&L."""

import math
from statistics import NormalDist

import numpy as np
import pytest

from finresearch.fincalc import black76 as b76
from finresearch.fincalc import options as o
from finresearch.fincalc import smile as sm
from finresearch.fincalc import svi

N = NormalDist()
T = 21 / 365
FLAT = svi.Raw(a=0.18**2 * T, b=0.0, m=0.0, rho=0.0, s=0.1)  # sigma = 18 % everywhere
SKEW = svi.Raw(a=0.0008, b=0.02, m=0.02, rho=-0.6, s=0.05)  # an index-like put skew, arbitrage-free
assert svi.check(SKEW).ok


def _put(p: svi.Raw, k: float) -> float:  # undiscounted Black-76 put on F = 1 at the smile's own volatility
    return b76.price("put", 1.0, math.exp(k), T, 1.0, sm.sigma(p, k, T))


# --------------------------------------------------------------------------- density and probabilities
def test_flat_smile_is_the_lognormal() -> None:
    wv = 0.18**2 * T
    for k in (-0.2, -0.05, 0.0, 0.07):
        z = (k + wv / 2) / math.sqrt(wv)  # ln(F_T/F) ~ N(-w/2, w)
        assert sm.density(FLAT, k) == pytest.approx(N.pdf(z) / math.sqrt(wv), rel=1e-12)
        assert sm.cdf(FLAT, k) == pytest.approx(N.cdf(z), abs=1e-14)


def test_cdf_and_density_match_finite_differences_of_put_prices() -> None:
    # Breeden-Litzenberger on F = 1: P(F_T < K) = dP/dK, q(K) = d2P/dK2; the density of k is q(K) K
    for k in (-0.15, -0.06, 0.0, 0.04, 0.1):
        kk, h = math.exp(k), 1e-4
        lo, mid, hi = _put(SKEW, math.log(kk - h)), _put(SKEW, k), _put(SKEW, math.log(kk + h))
        assert sm.cdf(SKEW, k) == pytest.approx((hi - lo) / (2 * h), abs=1e-6)
        assert sm.density(SKEW, k) == pytest.approx((hi - 2 * mid + lo) / h**2 * kk, rel=1e-4, abs=1e-6)


def test_density_integrates_to_one_and_is_a_martingale() -> None:
    kk = np.linspace(-2.0, 2.0, 20001)
    ps = np.array([sm.density(SKEW, float(k)) for k in kk])
    assert np.trapezoid(ps, kk) == pytest.approx(1.0, abs=1e-6)
    assert np.trapezoid(ps * np.exp(kk), kk) == pytest.approx(1.0, abs=1e-6)  # E[F_T] = F


# --------------------------------------------------------------------------- 25-delta risk reversal and butterfly
def test_flat_smile_rr_bf_by_hand() -> None:
    got = sm.rr_bf(FLAT, T)
    sd = math.sqrt(0.18**2 * T)
    # forward delta N(d1) = 0.25 with d1 = -k/sqrt(w) + sqrt(w)/2: k = sqrt(w) (0.67448975 + sqrt(w)/2)
    assert got.k_call == pytest.approx(sd * (0.6744897501960817 + sd / 2), abs=1e-10)
    assert got.k_put == pytest.approx(sd * (-0.6744897501960817 + sd / 2), abs=1e-10)
    assert got.atm == pytest.approx(0.18) and got.rr == pytest.approx(0.0, abs=1e-12)
    assert got.bf == pytest.approx(0.0, abs=1e-12)


def test_skew_rr_bf_against_the_deltas() -> None:
    got = sm.rr_bf(SKEW, T)
    assert got.reason is None and got.rr is not None and got.rr > 0  # puts dearer than calls
    # independent: at the returned strikes the Black-76 forward deltas (DF = 1) are 0.25 and -0.25
    kc, kp = got.k_call, got.k_put
    dc = b76.forward_delta("call", 1.0, math.exp(kc), T, 1.0, sm.sigma(SKEW, kc, T))
    dp = b76.forward_delta("put", 1.0, math.exp(kp), T, 1.0, sm.sigma(SKEW, kp, T))
    assert dc == pytest.approx(0.25, abs=1e-9) and dp == pytest.approx(-0.25, abs=1e-9)
    put, call, atm = sm.sigma(SKEW, kp, T), sm.sigma(SKEW, kc, T), sm.sigma(SKEW, 0.0, T)
    assert got.rr == pytest.approx(put - call) and got.bf == pytest.approx((put + call) / 2 - atm)


# --------------------------------------------------------------------------- constant maturity
def test_constant_maturity_interpolates_total_variance_at_fixed_delta() -> None:
    t1, t2, tt = 7 / 365, 35 / 365, 30 / 365
    near = svi.Raw(a=0.20**2 * t1, b=0.0, m=0.0, rho=0.0, s=0.1)
    far = svi.Raw(a=0.25**2 * t2, b=0.0, m=0.0, rho=0.0, s=0.1)
    got = sm.constant_maturity([(t2, far), (t1, near)], tt)
    w = 0.04 * t1 + (0.0625 * t2 - 0.04 * t1) * (tt - t1) / (t2 - t1)  # by hand
    assert got.atm == pytest.approx(math.sqrt(w / tt), abs=1e-12)
    assert got.rr == pytest.approx(0.0, abs=1e-12) and got.bf == pytest.approx(0.0, abs=1e-12)
    out = sm.constant_maturity([(t1, near), (t2, far)], 40 / 365)
    assert out.atm is None and "extrapolat" in (out.reason or "")
    exact = sm.constant_maturity([(t1, near), (t2, far)], t2)
    assert exact.atm == pytest.approx(0.25)


# --------------------------------------------------------------------------- greeks
def test_vanna_and_volga_match_finite_differences() -> None:
    f, k, t, df, s, h = 100.0, 95.0, 0.1, 0.99, 0.2, 1e-5
    vanna = (
        b76.forward_delta("call", f, k, t, df, s + h) - b76.forward_delta("call", f, k, t, df, s - h)
    ) / (2 * h)
    volga = (b76.vega(f, k, t, df, s + h) - b76.vega(f, k, t, df, s - h)) / (2 * h)
    assert sm.vanna(f, k, t, df, s) == pytest.approx(vanna, rel=1e-6)
    assert sm.volga(f, k, t, df, s) == pytest.approx(volga, rel=1e-6)


def test_sticky_moneyness_delta_matches_repricing_with_the_smile() -> None:
    f, df, h = 100.0, 0.995, 1e-3
    for right, k in (("put", 94.0), ("call", 104.0)):

        def v(ff: float, right=right, k=k) -> float:
            return b76.price(right, ff, k, T, df, sm.sigma(SKEW, math.log(k / ff), T))

        want = (v(f + h) - v(f - h)) / (2 * h)
        assert sm.smile_delta(right, f, k, T, df, SKEW) == pytest.approx(want, abs=1e-7)


def test_hull_white_delta_needs_its_coefficients() -> None:
    got = sm.hull_white_delta(0.3, 12.0, 100.0, 0.25, None)
    assert got.value is None and "coefficients" in (got.reason or "")
    # delta + vega / (S sqrt T) (a + b delta + c delta^2) = 0.3 + 12/(100 x 0.5) (-0.1 + 0.2 x 0.3 + 0.5 x 0.09)
    assert sm.hull_white_delta(0.3, 12.0, 100.0, 0.25, (-0.1, 0.2, 0.5)).value == pytest.approx(
        0.3012, abs=1e-12
    )


# --------------------------------------------------------------------------- Bakshi-Kapadia-Madan moments (display)
def test_bkm_moments_of_a_flat_smile_are_normal() -> None:
    wv = 0.18**2 * T
    got = sm.bkm_moments(FLAT, T)
    # ln(F_T/F) ~ N(-w/2, w): mean -w/2, variance w, skewness 0, kurtosis 3
    assert got.mean == pytest.approx(-wv / 2, abs=1e-8) and got.var == pytest.approx(wv, rel=1e-5)
    assert got.skew == pytest.approx(0.0, abs=1e-3) and got.kurt == pytest.approx(3.0, abs=1e-3)


def test_bkm_moments_match_the_density_moments() -> None:
    # BKM spans the moments with out-of-the-money prices; the density gives them directly: two independent paths
    kk = np.linspace(-1.5, 1.5, 30001)
    ps = np.array([sm.density(SKEW, float(k)) for k in kk])
    mu = np.trapezoid(kk * ps, kk)
    var = np.trapezoid((kk - mu) ** 2 * ps, kk)
    skew = np.trapezoid((kk - mu) ** 3 * ps, kk) / var**1.5
    kurt = np.trapezoid((kk - mu) ** 4 * ps, kk) / var**2
    got = sm.bkm_moments(SKEW, T)
    assert got.var == pytest.approx(var, rel=1e-4) and got.skew == pytest.approx(skew, abs=2e-3)
    assert got.kurt == pytest.approx(kurt, rel=2e-3) and got.skew < 0


# --------------------------------------------------------------------------- the P&L at expiry under the smile
LEGS = [o.Leg("put", 95.0, 1.10, -50), o.Leg("put", 90.0, 0.35, 50)]  # a short put spread, lot 50


def test_outcome_on_a_flat_smile_equals_the_lognormal_outcome() -> None:
    spot, rate = 100.0, 0.065
    want = o.outcome(LEGS, spot, T, 0.18, rate, rate, 10.0, steps=4000)
    got = sm.outcome(LEGS, spot, T, rate, FLAT, 10.0)
    assert got.pop == pytest.approx(want.pop, abs=1e-9)
    assert got.ev == pytest.approx(
        want.ev, abs=0.05
    )  # two quadratures of the same integral (Rs, on a Rs 37.5 credit)


def test_outcome_on_the_skew_moves_probability_into_the_put_wing() -> None:
    spot, rate = 100.0, 0.065
    atm = sm.sigma(SKEW, 0.0, T)
    flat = o.outcome(LEGS, spot, T, atm, rate, rate, 10.0, steps=4000)
    got = sm.outcome(LEGS, spot, T, rate, SKEW, 10.0)
    # breakeven 95 - (37.5 - 10)/50 = 94.45 on F = spot e^(rT): PoP = 1 - P(F_T < 94.45), from the smile's own CDF
    fwd = spot * math.exp(rate * T)
    assert got.pop == pytest.approx(1 - sm.cdf(SKEW, math.log(94.45 / fwd)), abs=1e-9)
    assert got.pop < flat.pop  # the skew's fatter left tail
    dist = sm.pnl_distribution(LEGS, spot, T, rate, SKEW, 10.0)
    assert sum(p for _, p in dist) == pytest.approx(1.0)

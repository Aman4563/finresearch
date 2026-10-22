"""Arbitrage-free SVI smiles (#386, P3.3): raw SVI, its SVI-JW form, the Gatheral-Jacquier butterfly function g(k),
Lee's wing bound and the calendar check, against hand values and the published examples of Gatheral and Jacquier,
"Arbitrage-free SVI volatility surfaces", Quantitative Finance 14(1), 2014 (arXiv:1204.0646): Example 3.1 (Axel
Vogt's raw parameters, whose density is negative) and Example 5.1 (their SVI-JW values and repaired wing)."""

import math

import pytest

from finresearch.fincalc import svi

# Example 3.1, eq. (3.8), t = 1: (a, b, m, rho, sigma) = (-0.0410, 0.1331, 0.3586, 0.3060, 0.4153)
VOGT = svi.Raw(a=-0.0410, b=0.1331, m=0.3586, rho=0.3060, s=0.4153)


# --------------------------------------------------------------------------- raw SVI by hand
def test_raw_values_by_hand() -> None:
    p = svi.Raw(a=0.04, b=0.1, m=0.0, rho=0.0, s=0.1)
    assert svi.w(p, 0.0) == pytest.approx(0.05, abs=1e-15)  # 0.04 + 0.1 x 0.1
    assert svi.w(p, 0.1) == pytest.approx(0.04 + 0.1 * math.sqrt(0.02), abs=1e-15)
    q = svi.Raw(a=0.04, b=0.1, m=0.1, rho=-0.5, s=0.1)
    # k = 0: 0.04 + 0.1 [-0.5 x -0.1 + sqrt(0.01 + 0.01)] = 0.0591421356...
    assert svi.w(q, 0.0) == pytest.approx(0.05914213562373095, abs=1e-15)
    # w' = b [rho + (k - m) / sqrt((k - m)^2 + s^2)] = 0.1 (-0.5 - 0.70710678) = -0.12071067811865475
    assert svi.dw(q, 0.0) == pytest.approx(-0.12071067811865475, abs=1e-15)
    # w'' = b s^2 / ((k - m)^2 + s^2)^(3/2) = 0.001 / 0.02^1.5 = 0.35355339059327373
    assert svi.d2w(q, 0.0) == pytest.approx(0.35355339059327373, abs=1e-13)


def test_flat_smile_has_g_one() -> None:
    # b = 0: w = a everywhere, w' = w'' = 0, so g = 1 (the lognormal)
    p = svi.Raw(a=0.04, b=0.0, m=0.0, rho=0.0, s=0.1)
    assert svi.g(p, -0.7) == pytest.approx(1.0) and svi.g(p, 0.3) == pytest.approx(1.0)


def test_g_matches_its_definition_by_finite_differences() -> None:
    # g = (1 - k w'/(2w))^2 - (w'^2/4)(1/w + 1/4) + w''/2 with w', w'' from central differences of w (independent path)
    h = 1e-4
    for k in (-1.2, -0.3, 0.0, 0.45, 1.1):
        w0 = svi.w(VOGT, k)
        w1 = (svi.w(VOGT, k + h) - svi.w(VOGT, k - h)) / (2 * h)
        w2 = (svi.w(VOGT, k + h) - 2 * w0 + svi.w(VOGT, k - h)) / h**2
        want = (1 - k * w1 / (2 * w0)) ** 2 - w1**2 / 4 * (1 / w0 + 0.25) + w2 / 2
        assert svi.g(VOGT, k) == pytest.approx(want, abs=1e-6)


# --------------------------------------------------------------------------- SVI-JW (GJ 2014 eq. 3.5, Lemma 3.2)
def test_raw_to_jw_matches_gatheral_jacquier_example_5_1() -> None:
    # Example 5.1: (v, psi, p, c, v~) = (0.01742625, -0.1752111, 0.6997381, 1.316798, 0.0116249)
    jw = svi.to_jw(VOGT, 1.0)
    assert jw.v == pytest.approx(0.01742625, abs=5e-9)
    assert jw.psi == pytest.approx(-0.1752111, abs=5e-8)
    assert jw.p == pytest.approx(0.6997381, abs=5e-8)
    assert jw.c == pytest.approx(1.316798, abs=5e-7)
    assert jw.v_min == pytest.approx(0.0116249, abs=5e-8)


def test_jw_round_trip() -> None:
    for t in (0.02, 0.25, 1.0):
        back = svi.from_jw(svi.to_jw(VOGT, t), t)
        assert back is not None
        for name in ("a", "b", "m", "rho", "s"):
            assert getattr(back, name) == pytest.approx(getattr(VOGT, name), abs=1e-10)


# --------------------------------------------------------------------------- static arbitrage checks
def test_vogt_smile_has_butterfly_arbitrage() -> None:
    # GJ Figure 1: g < 0 in the call wing. Independently (numpy, 600,001 points on [-3, 3]): g < 0 on about
    # [0.6424, 1.2569], minimum -0.032864 at k = 0.87926
    chk = svi.check(VOGT)
    assert chk.ok is False and "butterfly" in (chk.reason or "")
    assert chk.g_min == pytest.approx(-0.032864, abs=2e-5) and chk.g_min_k == pytest.approx(0.8793, abs=2e-3)
    assert chk.lee_ok  # b (1 + |rho|) = 0.1738 <= 2: the wings are fine, the density is not


def test_gatheral_jacquier_repaired_smile_is_free() -> None:
    # Example 5.1: keep (v, psi, p, v~), set the call wing c* = 0.8564763 -> free of butterfly arbitrage
    jw = svi.to_jw(VOGT, 1.0)
    fixed = svi.from_jw(svi.Jw(jw.v, jw.psi, jw.p, 0.8564763, 0.0116249), 1.0)
    assert fixed is not None
    chk = svi.check(fixed)
    assert chk.ok and chk.reason is None and chk.g_min >= 0
    # and the "guaranteed" pair (c, v~) = (0.3493158, 0.01548182) too
    safe = svi.from_jw(svi.Jw(jw.v, jw.psi, jw.p, 0.3493158, 0.01548182), 1.0)
    assert safe is not None and svi.check(safe).ok


def test_lee_wing_bound_and_negative_variance_are_refused() -> None:
    steep = svi.Raw(a=0.01, b=1.8, m=0.0, rho=0.2, s=0.1)  # b (1 + |rho|) = 2.16 > 2
    chk = svi.check(steep)
    assert chk.ok is False and chk.lee_ok is False and "Lee" in (chk.reason or "")
    neg = svi.Raw(a=-0.05, b=0.1, m=0.0, rho=0.0, s=0.1)  # min w = a + b s = -0.04
    chk2 = svi.check(neg)
    assert chk2.ok is False and "negative total variance" in (chk2.reason or "")


def test_calendar_check() -> None:
    near = svi.Raw(a=0.01, b=0.05, m=0.0, rho=-0.3, s=0.1)
    far = svi.Raw(a=0.02, b=0.05, m=0.0, rho=-0.3, s=0.1)  # near + 0.01 everywhere
    assert svi.calendar_ok(near, far).ok
    # a steeper near put wing crosses the far slice at k far enough below 0: calendar arbitrage there
    steep = svi.Raw(a=0.01, b=0.15, m=0.0, rho=-0.6, s=0.1)
    got = svi.calendar_ok(steep, far)
    assert got.ok is False and got.worst_k is not None and got.worst_k < 0 and got.worst > 0
    # by hand at k = -1: steep 0.01 + 0.15 (0.6 + sqrt(1.01)) = 0.2507...; far 0.02 + 0.05 (0.3 + 1.00499) = 0.0852
    assert svi.w(steep, -1.0) - svi.w(far, -1.0) == pytest.approx(0.2507481343 - 0.0852493781, abs=1e-9)

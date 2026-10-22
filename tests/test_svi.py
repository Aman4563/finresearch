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


# --------------------------------------------------------------------------- calibration (Zeliade quasi-explicit)
def test_nelder_mead_finds_the_rosenbrock_minimum() -> None:
    def rosen(x: list[float]) -> float:
        return (1 - x[0]) ** 2 + 100 * (x[1] - x[0] ** 2) ** 2

    x, fx = svi.nelder_mead(rosen, [-1.2, 1.0], step=0.5, max_iter=2000, ftol=1e-16)
    assert x[0] == pytest.approx(1.0, abs=1e-4) and x[1] == pytest.approx(1.0, abs=1e-4) and fx < 1e-8


def _brute_inner(ks, ws, wt, m, s):
    # independent: profile a out and scan (c, d) over a fine grid of the admissible quadrilateral
    import numpy as np

    y = (np.asarray(ks) - m) / s
    z = np.sqrt(y * y + 1)
    best = math.inf
    r = svi.RHO_MAX
    for c in np.linspace(0, 2 * s, 801):
        dmax = min(r * c, 2 * s - c)
        for d in np.linspace(-dmax, dmax, 201):
            res = np.asarray(ws) - d * y - c * z
            a = np.sum(wt * res) / np.sum(wt)
            best = min(best, float(np.sum(wt * (res - a) ** 2)))
    return best


@pytest.mark.parametrize("m,s", [(0.0, 0.1), (0.3, 0.02), (-0.4, 0.5)])
def test_inner_least_squares_is_the_constrained_optimum(m: float, s: float) -> None:
    # data from a smile the box at (m, s) may not contain: the inner answer must be no worse than any admissible point
    ks = [-0.3 + 0.05 * i for i in range(13)]
    ws = [float(svi.w(svi.Raw(0.01, 0.4, 0.05, -0.7, 0.08), k)) for k in ks]
    wt = [1.0] * len(ks)
    got = svi.inner(ks, ws, wt, m, s)
    brute = _brute_inner(ks, ws, wt, m, s)
    assert got.sse <= brute + 1e-12 and got.sse == pytest.approx(brute, rel=2e-2, abs=1e-12)
    c, d = got.c, got.d
    assert 0 <= c <= 2 * s + 1e-15 and abs(d) <= svi.RHO_MAX * c + 1e-15 and abs(d) <= 2 * s - c + 1e-15


def _points(p, t, ks, noise=None):
    out = []
    for i, k in enumerate(ks):
        sig = math.sqrt(float(svi.w(p, k)) / t) + (noise[i] if noise else 0.0)
        out.append(svi.Point(k, sig))
    return out


GOOD = svi.Raw(a=0.0004, b=0.012, m=0.01, rho=-0.55, s=0.03)  # a weekly-like smile, T = 7/365: ATM ~ 15 %


def test_fit_recovers_known_parameters() -> None:
    t = 7 / 365
    ks = [-0.08 + 0.008 * i for i in range(21)]
    got = svi.fit(_points(GOOD, t, ks), t)
    assert got.params is not None and got.reason is None
    assert got.rmse_vol < 1e-6 and got.n == 21
    for k in (-0.08, 0.0, 0.05):
        assert float(svi.w(got.params, k)) == pytest.approx(float(svi.w(GOOD, k)), rel=1e-5)


def test_fit_reports_rmse_in_vol_points_and_refuses_a_bad_fit() -> None:
    t = 7 / 365
    ks = [-0.08 + 0.008 * i for i in range(21)]
    alt = [
        0.004 if i % 2 else -0.004 for i in range(21)
    ]  # +/- 0.4 vol points, alternating: SVI cannot follow it
    got = svi.fit(_points(GOOD, t, ks, alt), t)
    assert got.params is not None and 0.002 < got.rmse_vol < 0.0045  # about 0.4 points left over
    big = [0.03 if i % 2 else -0.03 for i in range(21)]
    bad = svi.fit(_points(GOOD, t, ks, big), t)
    assert bad.params is None and "RMSE" in (bad.reason or "") and bad.rmse_vol > svi.MAX_RMSE


def test_fit_with_too_few_points_is_unknown() -> None:
    got = svi.fit(_points(GOOD, 0.02, [-0.02, 0.0, 0.02, 0.04]), 0.02)
    assert got.params is None and "fewer than" in (got.reason or "")


def test_fit_to_vogt_data_is_unknown_not_clipped() -> None:
    # Vogt's smile is inside the calibration box, so the fit reproduces it, and its density is negative: unknown
    ks = [-1.0 + 0.1 * i for i in range(25)]
    got = svi.fit(_points(VOGT, 1.0, ks), 1.0)
    assert got.params is None and got.raw is not None
    assert "butterfly" in (got.reason or "") and got.check is not None and got.check.g_min < 0


# --------------------------------------------------------------------------- SSVI (GJ 2014 section 4)
def test_power_law_phi_by_hand() -> None:
    # GJ (4.5): phi = eta / (theta^gamma (1 + theta)^(1 - gamma)); eta 1, gamma 1/2, theta 0.04: 1/(0.2 sqrt(1.04))
    assert svi.phi(0.04, 1.0, 0.5) == pytest.approx(4.902903378454601, abs=1e-12)


def test_ssvi_slice_is_a_raw_svi_slice() -> None:
    theta, rho, ph = 0.01, -0.6, 8.0
    raw = svi.ssvi_to_raw(theta, rho, ph)
    # w(0) = theta: the ATM total variance
    assert svi.ssvi_w(0.0, theta, rho, ph) == pytest.approx(theta, abs=1e-15)
    for k in (-0.4, -0.05, 0.0, 0.1, 0.3):
        # w = theta/2 [1 + rho phi k + sqrt((phi k + rho)^2 + 1 - rho^2)] by hand
        want = theta / 2 * (1 + rho * ph * k + math.sqrt((ph * k + rho) ** 2 + 1 - rho * rho))
        assert svi.ssvi_w(k, theta, rho, ph) == pytest.approx(want, abs=1e-15)
        assert float(svi.w(raw, k)) == pytest.approx(want, abs=1e-14)


def test_ssvi_butterfly_conditions() -> None:
    # theta phi (1 + |rho|) = 0.04 x 10 x 1.5 = 0.6 < 4; theta phi^2 (1 + |rho|) = 0.04 x 100 x 1.5 = 6 > 4
    ok, why = svi.ssvi_conditions(0.04, -0.5, 10.0)
    assert ok is False and "theta phi^2" in (why or "")
    assert svi.ssvi_conditions(0.04, -0.5, 5.0)[0]  # 0.3 and 1.5: both hold


def _ssvi_points(theta, rho, ph, t, ks):
    return [svi.Point(k, math.sqrt(svi.ssvi_w(k, theta, rho, ph) / t)) for k in ks]


def test_ssvi_surface_fit_recovers_parameters() -> None:
    rho, eta, gam = -0.65, 0.9, 0.4
    ts = (14 / 365, 42 / 365, 77 / 365)
    thetas = (0.0006, 0.0017, 0.0031)
    ks = [-0.12 + 0.02 * i for i in range(13)]  # 0 is on the grid: theta read from the data exactly
    slices = [
        (t, _ssvi_points(th, rho, svi.phi(th, eta, gam), t, ks)) for t, th in zip(ts, thetas, strict=True)
    ]
    got = svi.fit_ssvi(slices)
    assert got.reason is None and got.rho == pytest.approx(rho, abs=1e-4)
    assert got.eta == pytest.approx(eta, rel=1e-3) and got.gamma == pytest.approx(gam, abs=2e-3)
    assert [f.params is not None for f in got.slices] == [True, True, True]
    assert got.rmse_vol is not None and got.rmse_vol < 1e-5


def test_ssvi_surface_with_falling_atm_variance_is_unknown() -> None:
    ks = [-0.1 + 0.02 * i for i in range(11)]
    slices = [
        (0.05, _ssvi_points(0.002, -0.5, 5.0, 0.05, ks)),
        (0.1, _ssvi_points(0.0015, -0.5, 5.0, 0.1, ks)),
    ]
    got = svi.fit_ssvi(slices)
    assert got.rho is None and "calendar" in (got.reason or "")


# --------------------------------------------------------------------------- from quotes, and the flags for P1.6
def test_fit_quotes_through_the_black76_engine() -> None:
    from finresearch.fincalc import black76 as b76

    fwd, t, df = 22000.0, 7 / 365, 0.9988
    quotes = []
    for i in range(-15, 16):
        k = fwd * math.exp(0.006 * i)
        sig = math.sqrt(float(svi.w(GOOD, math.log(k / fwd))) / t)
        for right in ("call", "put"):
            px = b76.price(right, fwd, k, t, df, sig)
            quotes.append(b76.OptionQuote(k, right, bid=px - 0.05, ask=px + 0.05))
    quotes.append(b76.OptionQuote(fwd * 0.8, "put", bid=0.0, ask=0.05))  # no bid: dropped, counted
    got = svi.fit_quotes(quotes, fwd, t, df)
    assert got.fit.params is not None and got.fit.weights == "band"
    assert got.fit.rmse_vol is not None and got.fit.rmse_vol < 1e-4
    assert got.dropped.get("no bid") == 1
    for k in (-0.05, 0.0, 0.04):
        assert float(svi.w(got.fit.params, k)) == pytest.approx(float(svi.w(GOOD, k)), rel=1e-3)
    unknown = svi.fit_quotes(quotes, fwd, t, None)  # no discount factor: no IVs, no smile
    assert unknown.fit.params is None and "fewer than" in (unknown.fit.reason or "")


def test_surface_flags_name_every_violation() -> None:
    near = svi.Fit(svi.Raw(a=0.01, b=0.15, m=0.0, rho=-0.6, s=0.1), 0.05)
    far = svi.Fit(svi.Raw(a=0.02, b=0.05, m=0.0, rho=-0.3, s=0.1), 0.1)
    refused = svi.Fit(
        None, 0.2, reason="the fitted smile is not arbitrage-free: butterfly arbitrage: g = -0.03"
    )
    flags = svi.surface_flags([far, refused, near])
    kinds = sorted(f["kind"] for f in flags)
    assert kinds == ["calendar", "refused"]
    cal = next(f for f in flags if f["kind"] == "calendar")
    assert cal["t_near"] == 0.05 and cal["t_far"] == 0.1 and cal["k"] < 0 and cal["excess"] > 0


@pytest.mark.parametrize("p", [GOOD, svi.Raw(a=0.0002, b=0.03, m=0.0, rho=-0.8, s=0.02)])
def test_fit_settles_on_ticks_with_noise_like_a_weekly_index_chain(p: svi.Raw) -> None:
    # equal weights (settles carry no bid/ask, as addendum A4.2), strikes every 50 to +/- 10 %, settles rounded to
    # the 5-paisa tick and moved by up to two ticks: the fit must be known, arbitrage-free and under the 0.5-point target
    import random

    from finresearch.fincalc import black76 as b76

    rnd, fwd, t, df = random.Random(3), 22000.0, 7 / 365, 0.995
    quotes = []
    for i in range(-40, 41):
        k = round(fwd * (1 + 0.0025 * i) / 50) * 50
        sig = math.sqrt(float(svi.w(p, math.log(k / fwd))) / t)
        for right in ("call", "put"):
            px = round(b76.price(right, fwd, k, t, df, sig) / 0.05) * 0.05 + 0.05 * rnd.randint(-2, 2)
            if px > 0:
                quotes.append(b76.OptionQuote(k, right, price=px))
    got = svi.fit_quotes(quotes, fwd, t, df).fit
    assert got.params is not None and got.weights == "equal" and got.n >= 75
    assert got.rmse_vol is not None and got.rmse_vol < svi.TARGET_RMSE and got.check.g_min > 0

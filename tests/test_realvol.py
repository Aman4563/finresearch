"""Range and realised-variance estimators, losses and HAR (P3.7, #390; fincalc/realvol.py). Expected values are
computed independently: by hand from the published formulas (one bar O 100, H 110, L 95, C 105, previous close 98,
evaluated in a separate Python session and pinned here), by exact recovery on noise-free synthetic series, or by the
estimators' known unbiasedness on simulated Brownian paths."""

import math
from datetime import date, timedelta

import numpy as np
import pytest

from finresearch.fincalc import realvol as rv

BAR = rv.Bar(date(2025, 1, 2), 100.0, 110.0, 95.0, 105.0, 98.0)


# ----------------------------------------------------------------------------------------------- one bar
def test_parkinson_garman_klass_rogers_satchell_match_the_published_formulas_by_hand() -> None:
    # Parkinson 1980: (ln H/L)^2 / (4 ln 2); Garman-Klass 1980: 0.5 (ln H/L)^2 - (2 ln 2 - 1)(ln C/O)^2;
    # Rogers-Satchell 1991: ln(H/C) ln(H/O) + ln(L/C) ln(L/O)
    assert rv.parkinson(BAR) == pytest.approx(0.007751809156810511, rel=1e-12)
    assert rv.garman_klass(BAR) == pytest.approx(0.00982672327557351, rel=1e-12)
    assert rv.rogers_satchell(BAR) == pytest.approx(0.009567441357749173, rel=1e-12)
    assert rv.overnight(BAR) == pytest.approx(math.log(100 / 98))
    assert rv.close_to_close(BAR) == pytest.approx(math.log(105 / 98))
    assert rv.daily_variance(BAR) == pytest.approx(math.log(100 / 98) ** 2 + 0.009567441357749173)


@pytest.mark.parametrize(
    ("bar", "why"),
    [
        # a zero close is unknown, not dropped
        (rv.Bar(date(2025, 1, 2), 100.0, 110.0, 95.0, 0.0, 98.0), "close"),
        (rv.Bar(date(2025, 1, 2), None, 110.0, 95.0, 105.0, 98.0), "open"),
        (rv.Bar(date(2025, 1, 2), 100.0, 104.0, 95.0, 105.0, 98.0), "high"),  # H below C
        (rv.Bar(date(2025, 1, 2), 100.0, 110.0, 101.0, 105.0, 98.0), "low"),  # L above O
        (rv.Bar(date(2025, 1, 2), 100.0, 110.0, 95.0, 105.0, None), "previous close"),
    ],
)
def test_an_invalid_bar_is_unknown_with_a_reason(bar: rv.Bar, why: str) -> None:
    assert why in (rv.bar_problem(bar, need_prev=True) or "")
    assert rv.rogers_satchell(bar) is None or why == "previous close"
    assert rv.daily_variance(bar) is None
    assert rv.yang_zhang([BAR, bar, BAR]) is None  # a window holding an invalid bar is unknown


def test_yang_zhang_k_and_the_window_estimator_match_yang_and_zhang_2000() -> None:
    # k = 0.34 / (1.34 + (n + 1) / (n - 1)) (Yang and Zhang 2000, J. Business 73, 477-491, eq. 9 with alpha = 1.34)
    assert rv.yz_k(5) == pytest.approx(0.11971830985915495, rel=1e-12)
    assert rv.yz_k(22) == pytest.approx(0.1396167383652718, rel=1e-12)
    bars = [rv.Bar(date(2025, 1, 2 + i), o, h, lo, c, p) for i, (o, h, lo, c, p) in enumerate(
        [(100, 103, 99, 102, 101), (102.5, 104, 100, 101, 102), (100.5, 101, 97, 98, 101), (98.2, 99.5, 97.5, 99, 98)])]  # fmt: skip
    on = [math.log(b.open / b.prev_close) for b in bars]
    oc = [math.log(b.close / b.open) for b in bars]
    rs = [math.log(b.high / b.close) * math.log(b.high / b.open) + math.log(b.low / b.close) * math.log(b.low / b.open)
          for b in bars]  # fmt: skip
    k = 0.34 / (1.34 + 5 / 3)
    want = np.var(on, ddof=1) + k * np.var(oc, ddof=1) + (1 - k) * np.mean(rs)
    assert rv.yang_zhang(bars) == pytest.approx(want, rel=1e-12)
    assert rv.yang_zhang(bars[:1]) is None  # needs two bars
    cc = [math.log(b.close / b.prev_close) for b in bars]
    assert rv.close_to_close_variance(bars) == pytest.approx(np.var(cc, ddof=1), rel=1e-12)


def test_range_estimators_are_unbiased_on_simulated_brownian_bars_and_rogers_satchell_ignores_drift() -> None:
    rng = np.random.default_rng(390)
    sigma2, steps, days = 1e-4, 2000, 1500

    def bars(mu: float) -> list[rv.Bar]:
        inc = rng.normal(mu / steps, math.sqrt(sigma2 / steps), (days, steps))
        paths = np.concatenate([np.zeros((days, 1)), np.cumsum(inc, axis=1)], axis=1)
        d0 = date(2020, 1, 1)
        return [rv.Bar(d0 + timedelta(i), 100.0, 100 * math.exp(p.max()), 100 * math.exp(p.min()),
                       100 * math.exp(p[-1]), 100.0) for i, p in enumerate(paths)]  # fmt: skip

    flat = bars(0.0)
    for f in (rv.parkinson, rv.garman_klass, rv.rogers_satchell):
        # a discrete path under-reads the range by about 0.58 sqrt(1/steps) (Broadie and Glasserman 1997): about 2-4 % low at 2,000 steps
        assert np.mean([f(b) for b in flat]) == pytest.approx(sigma2, rel=0.06), f.__name__
    drift = bars(0.01)  # a 1 % daily drift: 100x the variance's square root
    assert np.mean([rv.rogers_satchell(b) for b in drift]) == pytest.approx(sigma2, rel=0.06)
    assert np.mean([rv.parkinson(b) for b in drift]) > 1.3 * sigma2  # Parkinson reads the drift as variance


# ----------------------------------------------------------------------------------------------- intraday
def test_realised_and_bipower_variance_and_the_jump_share_match_barndorff_nielsen_shephard() -> None:
    r = [0.01, -0.02, 0.015]
    assert rv.realized_variance(r, overnight=0.005) == pytest.approx(7.5e-4, rel=1e-12)
    assert rv.realized_variance(r) == pytest.approx(7.25e-4, rel=1e-12)
    assert rv.bipower_variation(r) == pytest.approx(math.pi / 2 * 5e-4, rel=1e-12)
    assert rv.jump_share(r) == 0.0  # BV above RV: no jump, never a negative share
    jumpy = [0.001] * 10 + [0.05]
    rv_, bv = 10e-6 + 0.0025, math.pi / 2 * (9e-6 + 5e-5)
    assert rv.jump_share(jumpy) == pytest.approx((rv_ - bv) / rv_, rel=1e-12)
    assert rv.realized_variance([0.01, None]) is None and rv.bipower_variation([0.01]) is None


# ----------------------------------------------------------------------------------------------- losses
def test_qlike_and_mse_by_hand_and_unknown_inputs() -> None:
    assert rv.qlike(2.0, 1.0) == pytest.approx(2 - math.log(2) - 1, rel=1e-12)
    assert rv.qlike(1.5, 1.5) == 0.0
    assert rv.mse(2.0, 1.0) == 1.0
    for a, f in ((0.0, 1.0), (1.0, 0.0), (None, 1.0), (1.0, None), (-1.0, 1.0)):
        assert rv.qlike(a, f) is None  # QLIKE is undefined there: unknown, not 0
    assert rv.mse(None, 1.0) is None


def test_mean_qlike_is_minimised_at_the_mean_of_the_proxy() -> None:
    # Patton (2011): QLIKE is a robust loss, so the expected loss is minimised by the conditional mean of RV
    a = np.random.default_rng(1).lognormal(-9, 0.6, 400)
    grid = np.linspace(0.5, 1.5, 201) * a.mean()
    best = grid[np.argmin([np.mean([rv.qlike(x, f) for x in a]) for f in grid])]
    assert best == pytest.approx(a.mean(), rel=0.006)


# ----------------------------------------------------------------------------------------------- vol cone
def test_vol_cone_reads_quantiles_of_annualised_window_variances_and_counts_unknown_windows() -> None:
    rng = np.random.default_rng(7)
    bars, c = [], 100.0
    for i in range(40):
        o = c * math.exp(rng.normal(0, 0.003))
        cl = o * math.exp(rng.normal(0, 0.01))
        bars.append(rv.Bar(date(2025, 1, 1) + timedelta(i), o, max(o, cl) * 1.004, min(o, cl) * 0.996, cl, c))
        c = cl
    bars[30] = rv.Bar(bars[30].day, 0.0, 1.0, 1.0, 1.0, 1.0)  # an invalid bar: windows over it are unknown
    cone = rv.vol_cone(bars, windows=(5, 10), quantiles=(0.1, 0.5, 0.9))
    w5 = [rv.yang_zhang(bars[i - 5 : i]) for i in range(5, 41)]
    known = [math.sqrt(252 * v) for v in w5 if v is not None]
    assert cone[5]["n"] == len(known) and cone[5]["unknown"] == 5
    for q in (0.1, 0.5, 0.9):
        assert cone[5]["q"][q] == pytest.approx(float(np.quantile(known, q)), rel=1e-12)
    assert cone[10]["unknown"] == 10


# ----------------------------------------------------------------------------------------------- OLS, HAR, MZ
def test_ols_matches_numpy_least_squares_and_reports_the_residual_variance() -> None:
    rng = np.random.default_rng(3)
    x = rng.normal(size=(60, 3))
    y = 0.2 + x @ np.array([0.5, -0.3, 0.1]) + rng.normal(0, 0.2, 60)
    fit = rv.ols(x.tolist(), y.tolist())
    xx = np.column_stack([np.ones(60), x])
    b, ssr, *_ = np.linalg.lstsq(xx, y, rcond=None)
    assert fit.coef == pytest.approx(b.tolist(), rel=1e-10)
    assert fit.s2 == pytest.approx(float(ssr[0]) / (60 - 4), rel=1e-10) and fit.n == 60
    assert rv.ols([[1.0], [1.0]], [1.0, 2.0]) is None  # fewer rows than coefficients + 1: unknown


def test_har_rows_use_the_day_week_and_month_means_and_the_forecast_is_bias_corrected() -> None:
    d = [float(i + 1) for i in range(30)]
    row = rv.har_row(d, 29)
    assert row == pytest.approx([math.log(30), math.log(np.mean(d[25:30])), math.log(np.mean(d[8:30]))])
    assert rv.har_row(d, 20) is None  # fewer than 22 sessions to t
    d[10] = None  # type: ignore[call-overload]
    assert rv.har_row(d, 29) is None  # an unknown day in the month window
    # noise-free: ln y = 0.1 + 0.3 x1 + 0.4 x2 + 0.2 x3 is recovered exactly and s^2 = 0
    rng = np.random.default_rng(5)
    x = rng.normal(-9, 0.5, (80, 3))
    ly = 0.1 + x @ np.array([0.3, 0.4, 0.2])
    fit = rv.ols(x.tolist(), ly.tolist())
    assert fit.coef == pytest.approx([0.1, 0.3, 0.4, 0.2], abs=1e-9) and fit.s2 == pytest.approx(0, abs=1e-20)
    # with lognormal noise, exp(x'b + s^2 / 2) is the mean of y (the bias correction), exp(x'b) its median
    x = rng.normal(-9, 0.5, (4000, 3))
    ly = 0.1 + x @ np.array([0.3, 0.4, 0.2]) + rng.normal(0, 0.5, 4000)
    fit = rv.ols(x.tolist(), ly.tolist())
    f = [rv.log_forecast(fit, list(r)) for r in x]
    assert np.mean(f) == pytest.approx(np.mean(np.exp(ly)), rel=0.03)
    assert np.mean([math.exp(math.log(v) - fit.s2 / 2) for v in f]) < 0.9 * np.mean(np.exp(ly))


def test_mincer_zarnowitz_recovers_a_and_b() -> None:
    f = [1.0, 2.0, 3.0, 4.0, 5.0]
    mz = rv.mincer_zarnowitz([0.5 + 0.8 * x for x in f], f)
    assert mz["a"] == pytest.approx(0.5) and mz["b"] == pytest.approx(0.8) and mz["r2"] == pytest.approx(1.0)
    assert mz["n"] == 5 and rv.mincer_zarnowitz([1.0], [1.0])["b"] is None


# ----------------------------------------------------------------------------------------------- GARCH
def test_garch_h_step_variance_sums_the_mean_reverting_path_by_hand() -> None:
    from finresearch.fincalc.taildist import GjrParams

    p = GjrParams(omega=1e-6, alpha=0.05, gamma=0.10, beta=0.85)  # persistence 0.05 + 0.05 + 0.85 = 0.95
    # E[s_t+k] = s_bar + 0.95^(k-1) (s_t+1 - s_bar), s_bar = 1e-6 / 0.05 = 2e-5; k = 1..3 from 4e-5
    assert rv.garch_horizon_variance(p, 4e-5, 3) == pytest.approx(
        6e-5 + (1 + 0.95 + 0.9025) * 2e-5, rel=1e-12
    )
    assert rv.garch_horizon_variance(p, 4e-5, 1) == pytest.approx(4e-5, rel=1e-12)
    assert rv.garch_horizon_variance(GjrParams(1e-6, 0.1, 0.0, 0.9), 4e-5, 3) is None  # not stationary


def garch_series(
    n: int = 600, seed: int = 390, omega: float = 2e-6, a: float = 0.08, b: float = 0.88
) -> np.ndarray:
    """GARCH(1,1) Gaussian returns (the scratch script's generator, verbatim)."""
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n)
    r, s2 = np.empty(n), omega / (1 - a - b)
    for t in range(n):
        r[t] = np.sqrt(s2) * z[t]
        s2 = omega + a * r[t] ** 2 + b * s2
    return r


def test_garch11_fit_matches_the_arch_package() -> None:
    # arch 8.0 arch_model(100 r, mean="Zero", vol="GARCH", p=1, q=1).fit(ftol=1e-12), run in a scratch environment
    # outside the repo: omega 3.962330795181204e-06 (rescaled), alpha 0.12061588502345971, beta 0.8143504706437775,
    # log-likelihood -3437.040324482172 on 100 r (+ 2 n ln 100 back on r)
    fit = rv.garch11_fit(garch_series().tolist(), window=600)
    assert fit.params is not None and fit.params.gamma == 0.0
    assert fit.params.omega == pytest.approx(3.962330795181204e-06, rel=2e-3)
    assert fit.params.alpha == pytest.approx(0.12061588502345971, rel=2e-3)
    assert fit.params.beta == pytest.approx(0.8143504706437775, rel=2e-3)
    assert fit.loglik == pytest.approx(-3437.040324482172 + 2 * 600 * math.log(100), abs=1e-4)
    short = rv.garch11_fit([0.01] * 50, window=600)
    assert short.params is None and "fewer" in (short.reason or "")

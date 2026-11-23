"""A first slice of the scipy / statsmodels comparisons (#503, scope item 4): OLS with Newey-West errors, the
cluster-robust t of a mean, the Wilson interval and the GPD maximum-likelihood fit. Still to do from that item:
two-way clustering, Lo's Sharpe standard error, BCa intervals and the quantile regression.

Conventions pinned first:
* `factor_math.ols_hac`: Bartlett weights 1 - l/(L+1), the n/(n-k) small-sample factor, lags as given:
  statsmodels `cov_type="HAC", cov_kwds={"maxlags": L, "use_correction": True}`;
* `metrics.clustered_t`: the t of the mean with one-way cluster-robust variance and the G/(G-1) factor: statsmodels
  OLS on a constant with `cov_type="cluster"` (its second factor (N-1)/(N-K) is 1 for a single regressor);
* Wilson: z = 1.96 in the module, so scipy's `proportion_ci` is asked for the level 2 Phi(1.96) - 1, not 0.95;
* GPD: location fixed at 0, shape xi and scale beta: `scipy.stats.genpareto.fit(x, floc=0)`.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import statsmodels.api as sm
from scipy import optimize, stats

from finresearch.backtest import metrics
from finresearch.fincalc import funds, signals
from finresearch.fincalc import taildist as td
from finresearch.portfolio import factor_math as fm


def test_ols_hac_matches_statsmodels(oracle):
    """Synthetic factor regressions with autocorrelated, heteroskedastic errors; n from 30 to 1,000, 1 to 5 factors,
    the automatic lag and fixed lags 0, 1 and 5. Both sides solve the same normal equations in double precision:
    1e-9 relative covers the conditioning of X'X (the module inverts it, statsmodels uses a pseudo-inverse)."""
    rng = np.random.default_rng(503)
    worst = {"coef": 0.0, "se": 0.0, "r2": 0.0}
    cases = 0
    for n, k in itertools.product([30, 60, 250, 1000], [1, 3, 5]):
        x = rng.standard_normal((n, k)) * 0.01 + 0.0003
        e = np.zeros(n)
        shock = rng.standard_normal(n) * 0.004 * (1 + 3 * np.abs(x[:, 0]) / 0.01)  # heteroskedastic
        for t in range(1, n):
            e[t] = 0.4 * e[t - 1] + shock[t]  # AR(1)
        y = 0.0002 + x @ np.linspace(0.5, 1.5, k) + e
        for lags in (None, 0, 1, 5):
            cases += 1
            got = fm.ols_hac(y, x, lags)
            ref = sm.OLS(y, sm.add_constant(x)).fit(
                cov_type="HAC", cov_kwds={"maxlags": got.lags, "use_correction": True}
            )
            assert got.lags == (fm.newey_west_lags(n) if lags is None else lags)
            worst["coef"] = max(worst["coef"], float(np.max(np.abs(np.array(got.coef) / ref.params - 1))))
            worst["se"] = max(worst["se"], float(np.max(np.abs(np.array(got.se) / ref.bse - 1))))
            worst["r2"] = max(worst["r2"], abs(got.r2 - ref.rsquared), abs(got.adj_r2 - ref.rsquared_adj))
    for name, diff in worst.items():
        oracle.check(f"factor_math.ols_hac ({name})", "statsmodels OLS, HAC with use_correction", grid=cases,
                     max_diff=diff, limit=1e-9, why="relative (conditioning of X'X)")  # fmt: skip


def test_clustered_t_matches_statsmodels(oracle):
    """Trade P&L clustered by expiry: 5 to 60 clusters of uneven size with a shared shock per cluster."""
    rng = np.random.default_rng(7)
    worst, cases = 0.0, 0
    for g in (5, 12, 30, 60):
        for _ in range(5):
            cases += 1
            sizes = rng.integers(1, 9, g)
            clusters = np.repeat(np.arange(g), sizes)
            values = 0.3 + rng.standard_normal(g)[clusters] + rng.standard_normal(len(clusters)) * 2
            ref = sm.OLS(values, np.ones(len(values))).fit(cov_type="cluster", cov_kwds={"groups": clusters})
            got = metrics.clustered_t(values.tolist(), clusters.tolist())
            worst = max(worst, abs(got / float(ref.tvalues[0]) - 1))
    oracle.check("metrics.clustered_t", "statsmodels OLS on a constant, cov_type='cluster'", grid=cases,
                 max_diff=worst, limit=1e-10, why="relative (float rounding)")  # fmt: skip


def test_wilson_intervals_match_scipy(oracle):
    level = 2 * stats.norm.cdf(1.96) - 1
    worst, cases = 0.0, 0
    for n in (1, 2, 5, 10, 37, 250, 4000):
        for k in sorted({0, 1, n // 3, n // 2, n - 1, n}):
            if not 0 <= k <= n:
                continue
            cases += 1
            ci = stats.binomtest(k, n).proportion_ci(confidence_level=level, method="wilson")
            for lo, hi in (signals.wilson(k, n), funds.wilson_interval(k, n)):
                worst = max(worst, abs(lo - ci.low), abs(hi - ci.high))
    oracle.check("signals.wilson, funds.wilson_interval", "scipy binomtest.proportion_ci('wilson')", grid=cases,
                 max_diff=worst, limit=1e-12, why="absolute probability (float rounding)")  # fmt: skip


def _tight_simplex(func, x0, args=(), disp=0):
    """scipy's default fit stops at xtol 1e-4; run its Nelder-Mead to 1e-10 so the oracle is sharper than the code."""
    return optimize.fmin(func, x0, args=args, xtol=1e-10, ftol=1e-12, maxiter=20000, maxfun=20000, disp=0)


def test_gpd_fit_matches_scipy_mle(oracle):
    """Excesses drawn from GPDs with shapes from -0.3 (bounded tail) to 0.6 (very heavy), 60 to 2,000 points. The
    module maximises Grimshaw's profile likelihood (golden section to 1e-12 in theta); scipy runs its simplex from
    its own start (never from the module's answer), here to xtol 1e-10. Compared on the log-likelihood (1e-8: both
    optimisers' stopping rules, second order in the parameters) and on the parameters in units of their asymptotic
    standard error (1 + xi) / sqrt(n): within 1e-4 of one."""
    rng = np.random.default_rng(11)
    ll_gap, par_gap, cases = (0.0, None), (0.0, None), 0
    for xi, n in itertools.product([-0.3, -0.1, 0.0, 0.15, 0.3, 0.6], [60, 250, 2000]):
        cases += 1
        x = stats.genpareto.rvs(xi, scale=0.02, size=n, random_state=rng)
        got = td.gpd_fit(x)
        c, _, scale = stats.genpareto.fit(x, floc=0, optimizer=_tight_simplex)
        ref_ll = float(np.sum(stats.genpareto.logpdf(x, c, 0, scale)))
        own_ll = float(np.sum(stats.genpareto.logpdf(x, got.xi, 0, got.beta)))
        assert abs(own_ll - got.loglik) < 1e-8 * max(
            1.0, abs(own_ll)
        )  # the module reports the likelihood it reached
        ll_gap = max(ll_gap, (abs(ref_ll - got.loglik), (xi, n, got.loglik, ref_ll)), key=lambda t: t[0])
        se = (1 + max(got.xi, 0.0)) / math.sqrt(n)
        diff = max(abs(got.xi - c), abs(got.beta / scale - 1)) / se
        par_gap = max(par_gap, (diff, (xi, n, got.xi, c, got.beta, scale)), key=lambda t: t[0])
    oracle.check("taildist.gpd_fit (log-likelihood)", "scipy genpareto.fit(floc=0), simplex to 1e-10", grid=cases,
                 max_diff=ll_gap[0], limit=1e-8, why="absolute log-likelihood (both optimisers' stopping rules)",
                 worst=ll_gap[1])  # fmt: skip
    oracle.check("taildist.gpd_fit (xi, beta)", "scipy genpareto.fit(floc=0), simplex to 1e-10", grid=cases,
                 max_diff=par_gap[0], limit=1e-4, why="x the asymptotic standard error (1 + xi) / sqrt(n)",
                 worst=par_gap[1])  # fmt: skip

"""fincalc/taildist (GJR-GARCH fit, filter, EWMA, backcast) and fincalc/realvol.garch11_fit against arch.

Until now the GJR fit was checked only against a second likelihood written by the same author. arch (K. Sheppard) is
an independent implementation of the same model, so a shared misreading (the gamma/2 in the stationarity bound and in
the starting variance, the sign of the asymmetry indicator, the backcast) would show here.

Conventions pinned first:
* zero mean, normal innovations: `ZeroMean` + `GARCH(p=1, o=1, q=1)` (o=0 for plain GARCH), `rescale=False`;
* arch wants returns of order 1, so it is given 100 r. Then omega_arch = 1e4 omega and its log-likelihood is ours
  minus n ln(100) (a change of units, not of the model);
* the asymmetry term multiplies r^2 when r < 0 on both sides; the starting variance is
  omega + (alpha + gamma/2 + beta) x backcast, with arch's backcast (weights 0.94^i over the first 75 squares).

The comparison is in two steps so that a difference can be placed: (1) the LIKELIHOOD and the filtered variances at
the same, given parameters: pure arithmetic, tolerance is float rounding; (2) the FITS, where the two optimisers
(in-house Nelder-Mead, arch's SLSQP) stop at slightly different points of a flat optimum.
"""

from __future__ import annotations

import math

import numpy as np
from arch.univariate import GARCH, EWMAVariance, ZeroMean

from finresearch.fincalc import realvol
from finresearch.fincalc import taildist as td

SCALE = 100.0


def simulate(
    n: int, omega: float, alpha: float, gamma: float, beta: float, seed: int, df: float | None = None
):
    """A synthetic zero-mean GJR-GARCH(1,1) path (normal or unit-variance Student-t shocks). Not market data."""
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n) if df is None else rng.standard_t(df, n) * math.sqrt((df - 2) / df)
    s2 = omega / (1 - alpha - gamma / 2 - beta)
    r = np.empty(n)
    for i in range(n):
        r[i] = math.sqrt(s2) * z[i]
        s2 = omega + (alpha + gamma * (r[i] < 0)) * r[i] ** 2 + beta * s2
    return r


SERIES = {
    "index-like, strong leverage": simulate(1500, 2e-6, 0.03, 0.10, 0.88, seed=1),
    "fat tails (t5)": simulate(1500, 3e-6, 0.05, 0.08, 0.86, seed=2, df=5.0),
    "low persistence": simulate(1000, 4e-5, 0.10, 0.05, 0.50, seed=3),
    "short window": simulate(300, 2e-6, 0.06, 0.06, 0.85, seed=4),
    "no leverage": simulate(2000, 1e-6, 0.08, 0.0, 0.90, seed=5),
}
POINTS = [  # (omega / sample variance, alpha, gamma, beta): the truth-like region, its edges, and gamma = 0
    (0.03, 0.05, 0.05, 0.87),
    (0.10, 0.02, 0.15, 0.80),
    (0.50, 0.20, 0.00, 0.30),
    (0.01, 0.00, 0.10, 0.93),
    (0.02, 0.10, 0.30, 0.74),
]


def arch_model(r: np.ndarray, o: int = 1):
    return ZeroMean(SCALE * r, volatility=GARCH(p=1, o=o, q=1), rescale=False)


def to_ours(loglik_scaled: float, n: int) -> float:
    return loglik_scaled + n * math.log(SCALE)


def test_likelihood_and_filter_match_arch_at_given_parameters(oracle):
    """Step 1. Same parameters on both sides: the log-likelihood (a sum of n terms: 1e-11 relative is float
    rounding), every conditional variance of the filter and the one-step forecast (1e-10 relative)."""
    ll, var, fc, n = (0.0, None), (0.0, None), (0.0, None), 0
    for name, r in SERIES.items():
        v, bc = float(np.var(r)), td.backcast(r)
        for w, a, g, b in POINTS:
            n += 1
            p = td.GjrParams(w * v, a, g, b)
            res = arch_model(r).fix([p.omega * SCALE**2, a, g, b])
            ref = to_ours(res.loglikelihood, len(r))
            got = -td._gjr_nll(list(r), bc, p)
            ll = max(ll, (abs(got / ref - 1), (name, w, a, g, b, got, ref)), key=lambda x: x[0])
            f = td.gjr_filter(r, p)
            ref_var = np.asarray(res.conditional_volatility) ** 2 / SCALE**2
            var = max(
                var,
                (float(np.max(np.abs(np.array(f.sigma2) / ref_var - 1))), (name, w, a, g, b)),
                key=lambda x: x[0],
            )
            nxt = float(res.forecast(horizon=1).variance.iloc[-1, 0]) / SCALE**2
            fc = max(fc, (abs(f.next_sigma2 / nxt - 1), (name, w, a, g, b)), key=lambda x: x[0])
    oracle.check("taildist._gjr_nll (log-likelihood)", "arch ZeroMean + GARCH(1,1,1).fix", grid=n, max_diff=ll[0],
                 limit=1e-11, why="relative (float rounding of a sum)", worst=ll[1])  # fmt: skip
    oracle.check("taildist.gjr_filter (sigma^2 path)", "arch conditional_volatility^2", grid=n, max_diff=var[0],
                 limit=1e-10, why="relative (float rounding of a recursion)", worst=var[1])  # fmt: skip
    oracle.check("taildist.gjr_filter (next sigma^2)", "arch forecast(horizon=1)", grid=n, max_diff=fc[0],
                 limit=1e-10, why="relative (float rounding of a recursion)", worst=fc[1])  # fmt: skip


def test_backcast_and_ewma_match_arch(oracle):
    """arch's EWMA (RiskMetrics, lambda 0.94) starts from the same backcast; the module's list has one more value
    (the next session's forecast), compared with arch's one-step forecast."""
    worst, n = (0.0, None), 0
    for name, r in SERIES.items():
        n += 1
        res = ZeroMean(SCALE * r, volatility=EWMAVariance(0.94), rescale=False).fit(disp="off")
        ref = np.asarray(res.conditional_volatility) ** 2 / SCALE**2
        got = td.ewma_variance(r)
        assert len(got) == len(r) + 1 and abs(got[0] / td.backcast(r) - 1) < 1e-15
        nxt = float(res.forecast(horizon=1).variance.iloc[-1, 0]) / SCALE**2
        diff = max(float(np.max(np.abs(np.array(got[:-1]) / ref - 1))), abs(got[-1] / nxt - 1))
        worst = max(worst, (diff, name), key=lambda x: x[0])
    oracle.check("taildist.ewma_variance, backcast", "arch EWMAVariance(0.94)", grid=n, max_diff=worst[0],
                 limit=1e-10, why="relative (float rounding of a recursion)", worst=worst[1])  # fmt: skip


def _fit_gap(ours, res, n: int, o: int):
    """(arch's log-likelihood minus ours, largest parameter difference in units of arch's standard error)."""
    ref = np.asarray(res.params, float)
    theirs = [ref[0] / SCALE**2, ref[1], ref[2] if o else 0.0, ref[-1]]
    mine = [ours.params.omega, ours.params.alpha, ours.params.gamma, ours.params.beta]
    se = np.asarray(res.std_err, float)
    se = [se[0] / SCALE**2, se[1], se[2] if o else math.inf, se[-1]]
    in_se = max(abs(a - b) / s for a, b, s in zip(mine, theirs, se, strict=True))
    return to_ours(res.loglikelihood, n) - ours.loglik, in_se, mine, theirs


def test_gjr_and_garch_fits_reach_archs_optimum(oracle):
    """Step 2. Both sides maximise the likelihood that step 1 showed to be the same function, so the fits can only
    differ by where each optimiser stops. Two tolerances, each with its reason:
    * log-likelihood: the in-house Nelder-Mead stops when its simplex spans 1e-10 in the likelihood and arch's SLSQP
      is run to 1e-10 as well, so neither may sit more than 1e-8 below the other;
    * parameters: a flat optimum lets them move without changing the likelihood, so they are compared in units of
      arch's own standard error: within a thousandth of one.
    A boundary fit (gamma or alpha at 0, where arch's standard errors are not meaningful) is compared on the
    likelihood only. Ten fits: five synthetic paths, GJR and plain GARCH."""
    ll_low, ll_high, par, n, boundary = (0.0, None), (0.0, None), (0.0, None), 0, 0
    for o, fit in (
        (1, lambda r: td.gjr_fit(r, window=len(r))),
        (0, lambda r: realvol.garch11_fit(r, window=len(r))),
    ):
        for name, r in SERIES.items():
            n += 1
            ours = fit(r)
            assert ours.params is not None, (name, ours.reason)
            res = arch_model(r, o).fit(disp="off", tol=1e-10, options={"maxiter": 2000})
            assert res.convergence_flag == 0
            gap, in_se, mine, theirs = _fit_gap(ours, res, len(r), o)
            ll_low = max(ll_low, (gap, (name, o, mine, theirs)), key=lambda x: x[0])  # arch above us
            ll_high = max(ll_high, (-gap, (name, o, mine, theirs)), key=lambda x: x[0])  # we above arch
            if min(mine[1], mine[2] if o else 1.0, theirs[1], theirs[2] if o else 1.0) < 1e-4:
                boundary += 1
                continue
            par = max(par, (in_se, (name, o, mine, theirs)), key=lambda x: x[0])
            # the model's own constraint, and the gamma/2 in it, hold at the optimum
            assert ours.params.alpha + ours.params.gamma / 2 + ours.params.beta < 1
    oracle.check("taildist.gjr_fit, realvol.garch11_fit (log-likelihood)", "arch fit (SLSQP, tol 1e-10)", grid=n,
                 max_diff=max(ll_low[0], ll_high[0]), limit=1e-8, why="absolute log-likelihood (both optimisers stop at 1e-10)",
                 worst=(ll_low, ll_high))  # fmt: skip
    oracle.check("taildist.gjr_fit, realvol.garch11_fit (parameters)", "arch fit (SLSQP, tol 1e-10)",
                 grid=n - boundary, max_diff=par[0], limit=1e-3, why="x arch's standard error (a flat optimum)",
                 worst=par[1])  # fmt: skip

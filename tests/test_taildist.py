"""The tail-aware physical distribution (#387, fincalc/taildist.py). Expected values are computed independently: by
hand, by fincalc.options' lognormal integration, or by scipy 1.18.1 (an optimiser on a separately written GJR
likelihood, and scipy.stats.genpareto.fit) in a scratch script outside the repo, pinned here."""

import math

import numpy as np
import pytest

from finresearch.fincalc import options as o
from finresearch.fincalc import taildist as td


def gjr_series(n: int = 1500, seed: int = 387, omega: float = 2e-6, a: float = 0.03, g: float = 0.10,
               b: float = 0.88, nu: int = 6) -> np.ndarray:  # fmt: skip
    """GJR(1,1) returns with unit-variance Student-t(6) shocks (the scratch script's generator, verbatim)."""
    rng = np.random.default_rng(seed)
    z = rng.standard_t(nu, n) / np.sqrt(nu / (nu - 2))
    r, s2 = np.empty(n), omega / (1 - a - g / 2 - b)
    for t in range(n):
        r[t] = np.sqrt(s2) * z[t]
        s2 = omega + (a + g * (r[t] < 0)) * r[t] ** 2 + b * s2
    return r


# ----------------------------------------------------------------------------------------------- the z statistic
def test_terminal_z_inverts_the_iv_scaled_terminal_price() -> None:
    # S_T = F exp(-sigma^2 T / 2 + sigma sqrt(T) z): z = 1.5 at F 20,000, sigma 15 %, T 30/365
    f, s, t = 20000.0, 0.15, 30 / 365
    st = f * math.exp(-0.5 * s * s * t + s * math.sqrt(t) * 1.5)
    assert td.terminal_z(st, f, s, t) == pytest.approx(1.5, abs=1e-12)
    assert td.terminal_z(None, f, s, t) is None and td.terminal_z(st, f, None, t) is None
    assert td.terminal_z(st, f, 0.0, t) is None and td.terminal_z(st, f, s, 0.0) is None


def test_kappa_is_the_mean_square_and_unknown_below_the_minimum() -> None:
    assert td.kappa([1.0, -1.0, 2.0, 0.0]) == pytest.approx(6 / 4)
    assert td.kappa([]) is None


def test_iv_scaled_terminal_has_mean_forward_for_normal_z_and_kappa_one() -> None:
    z = np.array([-1.0, 1.0])
    st = td.iv_scaled_terminal(100.0, 0.2, 1.0, z, 1.0)
    assert st == pytest.approx([100 * math.exp(-0.02 - 0.2), 100 * math.exp(-0.02 + 0.2)])


# ------------------------------------------------------------------------------------------- expected shortfall
def test_expected_shortfall_matches_the_issue_formula_by_hand() -> None:
    # 40 equal weights, alpha 2.5 %: one whole worst outcome
    x = list(range(-20, 20))
    assert td.expected_shortfall(x, alpha=0.025) == pytest.approx(20.0)
    # 1,000 equal weights: the mean of the worst 25
    y = np.arange(1000.0) - 500
    assert td.expected_shortfall(y, alpha=0.025) == pytest.approx(-np.mean(y[:25]))
    # weights: [-10 (0.01), -4 (0.02), 5 (0.97)] -> -(1/0.025)(0.01 x -10 + 0.015 x -4) = 6.4
    assert td.expected_shortfall([5, -4, -10], [0.97, 0.02, 0.01], 0.025) == pytest.approx(6.4)


# --------------------------------------------------------------------------- outcome from a weighted sample
def test_sample_outcome_reproduces_the_lognormal_card_on_its_own_grid() -> None:
    legs = [o.Leg("put", 24000, 60.0, -75), o.Leg("put", 23500, 25.0, 75)]
    spot, t, vol, rate = 24300.0, 28 / 365, 0.13, 0.065
    ref = o.outcome(legs, spot, t, vol, rate, rate, 120.0, None, steps=4000)
    grid = o.lognormal_grid(spot, t, vol, rate, 4000)
    got = td.sample_outcome(legs, [p for p, _ in grid], t, rate, 120.0, None, [w for _, w in grid])
    assert got.ev == pytest.approx(ref.ev, abs=1e-6) and got.pop == pytest.approx(ref.pop, abs=2e-3)
    dist = o.pnl_distribution(legs, spot, t, vol, rate, 120.0, None, 4000)
    assert got.dots == pytest.approx(o.weighted_quantiles(dist, td.DOTS), abs=1e-6)
    assert got.es > 0 and got.es <= -min(p for p, _ in dist) + 1e-9


def test_sample_outcome_charges_the_exit_cost_like_the_card() -> None:
    legs = [o.Leg("call", 24500, 110.0, 75), o.Leg("call", 25000, 30.0, -75)]
    spot, t, vol, rate = 24300.0, 28 / 365, 0.13, 0.065

    def stt(p: float) -> float:  # 0.125 % of the long call's intrinsic value
        return 0.00125 * 75 * max(0.0, p - 24500)

    ref = o.outcome(legs, spot, t, vol, rate, rate, 80.0, stt, steps=4000)
    grid = o.lognormal_grid(spot, t, vol, rate, 4000)
    got = td.sample_outcome(legs, [p for p, _ in grid], t, rate, 80.0, stt, [w for _, w in grid])
    assert got.ev == pytest.approx(ref.ev, abs=1e-6)


# ------------------------------------------------------------------------------------- the stationary bootstrap
def test_stationary_bootstrap_draws_runs_of_geometric_mean_length() -> None:
    rng = np.random.default_rng(1)
    idx = td.stationary_bootstrap(100, 200_000, 3.0, rng)
    assert idx.min() >= 0 and idx.max() <= 99
    # a run continues with probability 1 - 1/3 (circularly): the share of steps i -> i + 1 mod n is 2/3
    cont = np.mean((idx[1:] - idx[:-1]) % 100 == 1)
    assert cont == pytest.approx(2 / 3 + (1 / 3) / 100, abs=0.005)  # a restart lands on i + 1 with chance 1/n
    assert np.bincount(idx, minlength=100).min() > 1500  # every expiry is drawn (uniform restarts)


def test_block_bootstrap_is_reproducible_by_seed() -> None:
    a = td.stationary_bootstrap(50, 10, 3.0, np.random.default_rng(7))
    b = td.stationary_bootstrap(50, 10, 3.0, np.random.default_rng(7))
    assert (a == b).all()


# ------------------------------------------------------------------------------------------- the GPD tail
def test_gpd_fit_matches_scipy_genpareto_fit() -> None:
    # a GPD(xi 0.2, beta 0.5) sample by the inverse CDF; scipy.stats.genpareto.fit(x, floc=0) on it (scipy 1.18.1)
    rng = np.random.default_rng(11)
    x = 0.5 * (np.power(rng.uniform(size=400), -0.2) - 1) / 0.2
    assert x[:3] == pytest.approx([1.2680091, 0.37257613, 0.26753498], rel=1e-6)
    assert x.sum() == pytest.approx(271.7804908802241, rel=1e-12)
    fit = td.gpd_fit(x)
    # the optimum is flat: the same point to 1e-4, and a log-likelihood no worse than scipy's (ours is 1.4e-7 higher)
    assert fit.xi == pytest.approx(0.12140640704348195, abs=1e-4)
    assert fit.beta == pytest.approx(0.5980037888060703, rel=1e-4)
    assert -242.89836162368726 - 1e-9 <= fit.loglik <= -242.89836162368726 + 1e-6


def test_gpd_tail_needs_its_minimum_exceedances() -> None:
    z = np.random.default_rng(3).standard_t(4, 400)
    tail = td.gpd_tail(z, q=0.95, min_exceed=15)
    assert tail is not None and tail.n_exceed == 20 and tail.threshold == pytest.approx(np.quantile(np.abs(z), 0.95))
    # scipy.stats.genpareto.fit(|z| - u over |z| > u, floc=0): a short-tailed fit (xi < 0) on 20 points
    assert tail.threshold == pytest.approx(2.8875516916763027, rel=1e-12)
    assert tail.xi == pytest.approx(-0.4490895487147928, abs=1e-4)
    assert tail.beta == pytest.approx(1.6515613896073926, rel=1e-4)
    assert td.gpd_tail(z[:100], q=0.95, min_exceed=15) is None  # 5 exceedances: no tail, never a noisy fit


# ------------------------------------------------------------------------------------------- filters
def test_nelder_mead_finds_the_rosenbrock_minimum() -> None:
    res = td.nelder_mead(lambda p: (1 - p[0]) ** 2 + 100 * (p[1] - p[0] ** 2) ** 2, [-1.2, 1.0], [0.1, 0.1])
    assert res.converged and res.x == pytest.approx([1.0, 1.0], abs=1e-5)


def test_ewma_variance_by_hand() -> None:
    # sigma^2_t = 0.94 sigma^2_{t-1} + 0.06 r^2_{t-1}, from a given start
    got = td.ewma_variance([0.01, -0.02], lam=0.94, start=1e-4)
    assert got == pytest.approx([1e-4, 0.94e-4 + 0.06e-4, 0.94 * 1e-4 + 0.06 * 4e-4])


def test_backcast_is_archs() -> None:
    r = np.array([0.01, -0.02, 0.03])
    w = 0.94 ** np.arange(3)
    assert td.backcast(r) == pytest.approx(float(np.sum(w / w.sum() * r**2)))


def test_gjr_filter_by_hand() -> None:
    p = td.GjrParams(omega=1e-6, alpha=0.05, gamma=0.1, beta=0.9)
    r = [0.01, -0.02]
    bc = td.backcast(np.array(r))
    s0 = 1e-6 + (0.05 + 0.1 / 2 + 0.9) * bc
    s1 = 1e-6 + 0.05 * 1e-4 + 0.9 * s0
    s2 = 1e-6 + (0.05 + 0.1) * 4e-4 + 0.9 * s1  # the next session's forecast
    got = td.gjr_filter(r, p)
    assert got.sigma2 == pytest.approx([s0, s1]) and got.next_sigma2 == pytest.approx(s2)
    assert got.z == pytest.approx([0.01 / math.sqrt(s0), -0.02 / math.sqrt(s1)])


def test_gjr_fit_matches_an_independent_scipy_fit() -> None:
    # scipy 1.18.1 SLSQP on a separately written Gaussian GJR likelihood with arch's backcast, two starts, pinned
    r = gjr_series()
    assert r[:3] == pytest.approx([-0.00864976, -0.00692306, 0.00333252], rel=1e-5)
    fit = td.gjr_fit(r, window=len(r))  # the whole series, as the reference
    assert fit.params is not None and fit.reason is None
    assert fit.loglik >= 5385.174137567787 - 1e-4
    p = fit.params
    assert p.omega == pytest.approx(1.6845710482056098e-06, rel=0.02)
    assert (p.alpha, p.gamma, p.beta) == pytest.approx((0.04770763893572895, 0.05671467991330568,
                                                        0.8911758464038406), abs=2e-3)  # fmt: skip


def test_gjr_fit_refuses_a_short_series() -> None:
    fit = td.gjr_fit(gjr_series()[:100], min_returns=250)
    assert fit.params is None and "returns" in fit.reason


def test_gjr_fit_uses_only_the_last_window_returns() -> None:
    r = gjr_series()
    assert td.gjr_fit(r, window=1000).loglik == pytest.approx(td.gjr_fit(r[-1000:], window=1000).loglik)

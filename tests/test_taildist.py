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
    idx = td.stationary_bootstrap(100, 60_000, 3.0, rng)
    assert idx.min() >= 0 and idx.max() <= 99
    # a run continues with probability 1 - 1/3 (circularly): the share of steps i -> i + 1 mod n is 2/3
    cont = np.mean((idx[1:] - idx[:-1]) % 100 == 1)
    assert cont == pytest.approx(2 / 3 + (1 / 3) / 100, abs=0.01)  # a restart lands on i + 1 with chance 1/n
    assert np.bincount(idx, minlength=100).min() > 400  # every expiry is drawn (uniform restarts)


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
    assert (
        tail is not None
        and tail.n_exceed == 20
        and tail.threshold == pytest.approx(np.quantile(np.abs(z), 0.95))
    )
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
    assert p.omega == pytest.approx(1.6845710482056098e-06, rel=1e-4)
    assert (p.alpha, p.gamma, p.beta) == pytest.approx((0.04770763893572895, 0.05671467991330568,
                                                        0.8911758464038406), abs=1e-5)  # fmt: skip


def test_gjr_fit_refuses_a_short_series() -> None:
    fit = td.gjr_fit(gjr_series()[:100], min_returns=250)
    assert fit.params is None and "returns" in fit.reason


def test_gjr_fit_uses_only_the_last_window_returns() -> None:
    r = gjr_series()
    assert td.gjr_fit(r, window=1000).loglik == pytest.approx(td.gjr_fit(r[-1000:], window=1000).loglik)


# ------------------------------------------------------------------------------------------- M2: IV-scaled empirical
PCS = [o.Leg("put", 24000, 60.0, -75), o.Leg("put", 23500, 25.0, 75)]
F, SIG, T, R = 24400.0, 0.13, 28 / 365, 0.065


def normal_scores(n: int) -> np.ndarray:
    from statistics import NormalDist

    return np.array([NormalDist().inv_cdf((i + 0.5) / n) for i in range(n)])


def test_empirical_model_on_normal_z_is_the_lognormal_card_at_the_atm_iv() -> None:
    zs = normal_scores(4000)
    got = td.empirical_forecast(PCS, F, SIG, T, R, 120.0, None, zs, rng=np.random.default_rng(1), boot=50)
    # the grid's own kappa (0.9994), so the lognormal reference uses sigma sqrt(kappa); EV within Rs 2 of Rs 6,266:
    # 4,000 normal scores stop at |z| = 3.5 where the card's grid integrates to 6
    k = float(np.mean(zs**2))
    ref = o.outcome(PCS, F * math.exp(-R * T), T, SIG * math.sqrt(k), R, R, 120.0, None)
    assert got.kappa == pytest.approx(k) and got.n == 4000 and got.reason is None
    assert got.outcome.pop == pytest.approx(ref.pop, abs=1e-3) and got.outcome.ev == pytest.approx(
        ref.ev, abs=2.0
    )
    assert got.ev_lo <= got.outcome.ev <= got.ev_hi


def test_empirical_model_turns_fat_left_tails_into_a_lower_ev_for_a_put_seller() -> None:
    zs = normal_scores(400)
    fat = np.where(zs < -1.5, zs * 1.6, zs)  # the same body, a heavier left tail
    rng = np.random.default_rng(2)
    a = td.empirical_forecast(PCS, F, SIG, T, R, 120.0, None, zs, rng=rng, boot=50)
    b = td.empirical_forecast(PCS, F, SIG, T, R, 120.0, None, fat, rng=rng, boot=50)
    assert b.kappa > a.kappa and b.outcome.ev < a.outcome.ev and b.outcome.es > a.outcome.es


def test_empirical_model_is_unknown_without_enough_z_or_an_iv() -> None:
    zs = normal_scores(20)
    got = td.empirical_forecast(PCS, F, SIG, T, R, 120.0, None, zs, min_z=30, rng=np.random.default_rng(1))
    assert got.outcome is None and "20 z" in got.reason
    got = td.empirical_forecast(
        PCS, F, None, T, R, 120.0, None, normal_scores(100), rng=np.random.default_rng(1)
    )
    assert got.outcome is None and "IV" in got.reason


def test_ev_interval_narrows_with_more_expiries() -> None:
    z = np.random.default_rng(5).standard_t(5, 2000) / math.sqrt(5 / 3)
    small = td.empirical_forecast(
        PCS, F, SIG, T, R, 120.0, None, z[:100], rng=np.random.default_rng(1), boot=300
    )
    big = td.empirical_forecast(PCS, F, SIG, T, R, 120.0, None, z, rng=np.random.default_rng(1), boot=300)
    assert big.ev_hi - big.ev_lo < (small.ev_hi - small.ev_lo) / 2


def test_gpd_tail_draws_replace_only_the_exceedances() -> None:
    z = np.random.default_rng(3).standard_t(4, 400)
    tail = td.gpd_tail(z, q=0.95, min_exceed=15)
    draws = td.sample_z(z, 20000, np.random.default_rng(4), 3.0, tail)
    inside = np.abs(draws) <= tail.threshold
    assert set(np.round(draws[inside], 12)) <= set(np.round(z, 12))  # the body is resampled as is
    assert (
        np.mean(~inside) == pytest.approx(0.05, abs=0.01)
        and np.abs(draws).max() < tail.threshold - tail.beta / tail.xi + 1e-9
    )


# ------------------------------------------------------------------------------------------- M3: FHS
def test_fhs_with_iid_normal_residuals_and_constant_variance_is_the_lognormal_card() -> None:
    r = normal_scores(2000)[np.random.default_rng(8).permutation(2000)] * 0.01  # i.i.d.-like, unit residuals
    flat = td.GjrParams(omega=1e-4, alpha=0.0, gamma=0.0, beta=0.0)  # sigma^2 = 1e-4 every day
    got = td.fhs_forecast(
        PCS, F, SIG, T, R, 120.0, None, r, flat, steps=20, kap=1.0, rng=np.random.default_rng(3)
    )
    ref = o.outcome(PCS, F * math.exp(-R * T), T, SIG, R, R, 120.0, None)
    assert got.reason is None and got.outcome.n == td.PATHS
    # Monte Carlo error of 20,000 paths: PoP sd 0.0033, EV sd about Rs 60
    assert got.outcome.pop == pytest.approx(ref.pop, abs=0.012) and got.outcome.ev == pytest.approx(
        ref.ev, abs=200
    )


def test_fhs_paths_are_rescaled_to_kappa_times_the_iv_variance() -> None:
    r = gjr_series()
    p = td.gjr_fit(r).params
    paths = td.fhs_log_returns(r, p, steps=10, total_var=1.03 * SIG**2 * T, rng=np.random.default_rng(1))
    assert np.mean(np.sum(paths**2, axis=1)) == pytest.approx(1.03 * SIG**2 * T, rel=1e-9)


def test_fhs_on_a_leverage_filter_skews_the_terminal_left() -> None:
    r = gjr_series()
    p = td.gjr_fit(r).params
    x = td.fhs_log_returns(r, p, steps=20, total_var=SIG**2 * T, rng=np.random.default_rng(1)).sum(axis=1)
    skew = float(np.mean((x - x.mean()) ** 3) / np.std(x) ** 3)
    assert skew < -0.1  # gamma > 0: a fall raises the next variance


def test_fhs_also_runs_on_the_ewma_filter() -> None:
    r = gjr_series()
    x = td.fhs_log_returns(r, td.Ewma(), steps=5, total_var=SIG**2 * T, rng=np.random.default_rng(1))
    assert x.shape == (td.PATHS, 5)


def test_fhs_is_unknown_without_filter_parameters_or_steps() -> None:
    r = gjr_series()
    got = td.fhs_forecast(
        PCS, F, SIG, T, R, 120.0, None, r, None, steps=10, kap=1.0, rng=np.random.default_rng(1)
    )
    assert got.outcome is None and "filter" in got.reason
    p = td.GjrParams(1e-6, 0.05, 0.1, 0.85)
    got = td.fhs_forecast(
        PCS, F, SIG, T, R, 120.0, None, r, p, steps=0, kap=1.0, rng=np.random.default_rng(1)
    )
    assert got.outcome is None and "steps" in got.reason
    got = td.fhs_forecast(
        PCS, F, SIG, T, R, 120.0, None, r, p, steps=5, kap=None, rng=np.random.default_rng(1)
    )
    assert got.outcome is None and "kappa" in got.reason


def test_weekday_steps_count_sessions_after_the_origin_up_to_expiry() -> None:
    from datetime import date

    # Thursday 2-Jan-2025 close to Thursday 30-Jan-2025: 20 weekdays (holidays are not used: known at the origin)
    assert td.weekday_steps(date(2025, 1, 2), date(2025, 1, 30)) == 20


# ------------------------------------------------------------------------------------------- the robust EV gate
def model_ev(name: str, validated: bool, ev: float | None, lo: float | None, es: float | None) -> td.ModelEV:
    return td.ModelEV(name, validated, ev, lo, es)


def test_robust_gate_is_informational_until_a_model_is_validated() -> None:
    ms = [model_ev("M2", False, 900.0, 500.0, 3000.0)]
    for cur in (True, False):
        g = td.robust_gate(cur, ms, costs=150.0, lam=1.0, es_budget=5000.0)
        assert g.passed is cur and g.informational and g.ev_robust is None


def test_robust_gate_takes_the_worst_validated_model() -> None:
    ms = [model_ev("M2", True, 900.0, 400.0, 3000.0), model_ev("M3", True, 700.0, 120.0, 4200.0),
          model_ev("M1", False, -50.0, -400.0, 9000.0)]  # fmt: skip
    g = td.robust_gate(True, ms, costs=100.0, lam=1.0, es_budget=5000.0)
    assert g.passed and g.ev_robust == 700.0 and g.ev_lo_robust == 120.0 and g.es_worst == 4200.0
    assert not td.robust_gate(True, ms, costs=130.0, lam=1.0, es_budget=5000.0).passed  # 120 <= lam x costs
    assert not td.robust_gate(True, ms, costs=100.0, lam=1.0, es_budget=4000.0).passed  # ES over budget
    neg = [model_ev("M2", True, -10.0, -300.0, 100.0)]
    assert not td.robust_gate(
        True, neg, costs=0.0, lam=1.0, es_budget=5000.0
    ).passed  # lower bound must be > 0


def test_robust_gate_fails_on_any_unknown() -> None:
    ms = [model_ev("M2", True, 900.0, None, 3000.0)]
    assert not td.robust_gate(True, ms, costs=100.0, lam=1.0, es_budget=5000.0).passed
    ok = [model_ev("M2", True, 900.0, 400.0, 3000.0)]
    assert not td.robust_gate(True, ok, costs=None, lam=1.0, es_budget=5000.0).passed
    assert not td.robust_gate(True, ok, costs=100.0, lam=1.0, es_budget=None).passed


def test_robust_gate_is_never_more_permissive_than_the_current_gate() -> None:
    rng = np.random.default_rng(387)
    for _ in range(3000):
        k = int(rng.integers(0, 4))

        def val() -> float | None:
            return None if rng.random() < 0.1 else float(rng.normal(0, 1000))

        ms = [model_ev(f"M{i}", bool(rng.random() < 0.7), val(), val(), val()) for i in range(k)]
        cur = bool(rng.random() < 0.5)
        g = td.robust_gate(cur, ms, costs=val(), lam=float(rng.uniform(0, 3)), es_budget=val())
        assert not g.passed or cur


# ------------------------------------------------------------------------------------------- drawdown, VRP, Kelly
def test_max_drawdown_counts_from_the_start() -> None:
    assert td.max_drawdown([-1.0, 2.0, -3.0, 1.0]) == 3.0  # peak 1 after two trades, trough -2
    assert td.max_drawdown([-2.0, 1.0]) == 2.0 and td.max_drawdown([1.0, 1.0]) == 0.0


def test_block_bootstrap_drawdown_matches_exhaustive_enumeration() -> None:
    # 3 trades, blocks of 3 (circular): exactly three equally likely paths, by start index
    pnls = [-3.0, 2.0, -2.0]
    paths = [[-3.0, 2.0, -2.0], [2.0, -2.0, -3.0], [-2.0, -3.0, 2.0]]  # max drawdowns 3, 5, 5
    dds = sorted(td.max_drawdown(p) for p in paths)
    assert dds == [3.0, 5.0, 5.0]
    got = td.drawdown_bootstrap(
        pnls, capital=100.0, x=0.045, block=3, sims=30_000, rng=np.random.default_rng(1)
    )
    assert got.p_exceed == pytest.approx(np.mean([d > 4.5 for d in dds]), abs=0.01)
    assert got.dar95 == pytest.approx(max(dds)) and got.cdar95 == pytest.approx(max(dds))


def test_drawdown_of_a_steady_loser_is_certain() -> None:
    got = td.drawdown_bootstrap(
        [-1000.0] * 12, capital=100_000.0, x=0.05, horizon=10, rng=np.random.default_rng(1)
    )
    assert (
        got.p_exceed == 1.0 and got.dar95 == pytest.approx(10_000.0) and got.cdar95 == pytest.approx(10_000.0)
    )


def test_cdar_is_the_mean_of_the_worst_five_percent() -> None:
    pnls = list(np.random.default_rng(2).normal(100, 1000, 60))
    got = td.drawdown_bootstrap(pnls, capital=1e5, x=0.02, sims=4000, rng=np.random.default_rng(3), keep=True)
    dd = np.sort(got.drawdowns)
    assert got.dar95 == pytest.approx(float(np.quantile(dd, 0.95)))
    assert got.cdar95 == pytest.approx(float(np.mean(dd[dd >= np.quantile(dd, 0.95)])))


def test_vrp_and_kelly() -> None:
    assert td.vrp(500.0, -120.0, (100.0, 900.0)) == (620.0, 220.0, 1020.0)
    assert td.vrp(None, -120.0, (100.0, 900.0)) is None
    assert td.kelly_lo(-0.01, 0.04) == 0.0 and td.kelly_lo(0.02, 0.04) == pytest.approx(0.5)
    assert td.kelly_lo(0.02, 0.0) is None and td.kelly_lo(None, 0.04) is None


def test_the_even_money_risk_of_ruin_is_retired() -> None:
    # #387: an even-money formula with no callers; drawdown risk is drawdown_bootstrap's
    assert not hasattr(o, "risk_of_ruin")

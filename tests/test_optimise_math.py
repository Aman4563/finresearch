"""Portfolio optimiser arithmetic (#346): expectations computed independently (closed forms, brute force, loops)."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from finresearch.portfolio import optimise_math as om
from finresearch.portfolio.analytics_math import risk_contributions


def _lw_loop(x: np.ndarray) -> tuple[np.ndarray, float]:
    """Ledoit & Wolf (2004) eq. 14-15 written with explicit loops (the module is vectorised)."""
    n, p = x.shape
    xc = x - x.mean(axis=0)
    s = sum(np.outer(xc[k], xc[k]) for k in range(n)) / n
    mu = np.trace(s) / p
    d2 = sum((s[i, j] - (mu if i == j else 0.0)) ** 2 for i in range(p) for j in range(p)) / p
    b2 = sum(np.sum((np.outer(xc[k], xc[k]) - s) ** 2) for k in range(n)) / n**2 / p
    delta = min(b2, d2) / d2
    return delta * mu * np.eye(p) + (1 - delta) * s, delta


def test_ledoit_wolf_matches_the_paper_formula():
    rng = np.random.default_rng(7)
    x = rng.normal(size=(60, 4)) @ np.array([[1, 0.5, 0, 0], [0, 1, 0.3, 0], [0, 0, 1, 0.2], [0, 0, 0, 2.0]])
    cov, delta = om.ledoit_wolf(x)
    want, want_delta = _lw_loop(x)
    assert delta == pytest.approx(want_delta, rel=1e-12)
    assert np.allclose(cov, want, rtol=1e-12, atol=1e-15)


def test_ledoit_wolf_shrinks_noise_hard_and_a_strong_factor_little():
    rng = np.random.default_rng(1)
    _, noise = om.ledoit_wolf(rng.normal(size=(40, 30)))  # iid: the sample's off-diagonals are pure noise
    f = rng.normal(size=(500, 1))
    _, factor = om.ledoit_wolf(f @ np.ones((1, 5)) * 3 + rng.normal(size=(500, 5)) * 0.3)
    assert noise > 0.5
    assert factor < 0.1


def test_capped_simplex_projection_keeps_bounds_and_budget():
    w = om.project_capped_simplex(
        np.array([0.9, 0.5, -0.2]), np.array([0.0, 0.1, 0.0]), np.array([0.6, 1, 1])
    )
    assert w.sum() == pytest.approx(1.0, abs=1e-12)
    assert w[0] == pytest.approx(0.6)
    assert w[1] >= 0.1 - 1e-12 and w[2] >= -1e-12


def test_min_variance_two_assets_closed_form():
    s1, s2, rho = 0.2, 0.3, 0.25
    s12 = rho * s1 * s2
    cov = np.array([[s1**2, s12], [s12, s2**2]])
    w = om.min_variance(cov, np.zeros(2), np.ones(2))
    w1 = (s2**2 - s12) / (s1**2 + s2**2 - 2 * s12)
    assert w[0] == pytest.approx(w1, abs=1e-7)


def test_min_variance_three_assets_beats_a_brute_force_grid():
    cov = np.array([[0.04, 0.006, 0.01], [0.006, 0.09, -0.012], [0.01, -0.012, 0.0225]])
    lo, hi = np.zeros(3), np.array([1.0, 1.0, 0.4])  # the third asset's cap binds
    w = om.min_variance(cov, lo, hi)
    step = 0.005
    best = min(
        (float(v @ cov @ v), v)
        for a, b in itertools.product(np.arange(0, 1 + 1e-9, step), repeat=2)
        if (c := 1 - a - b) >= -1e-12 and c <= 0.4 + 1e-12
        for v in [np.array([a, b, max(c, 0.0)])]
    )
    assert float(w @ cov @ w) <= best[0] + 1e-12
    assert w[2] == pytest.approx(0.4, abs=1e-6)
    assert np.all(w >= -1e-9) and w.sum() == pytest.approx(1.0)


def test_risk_parity_two_assets_is_inverse_vol():
    cov = np.array([[0.04, 0.012], [0.012, 0.16]])
    w = om.risk_parity(cov)
    assert w[0] == pytest.approx((1 / 0.2) / (1 / 0.2 + 1 / 0.4), abs=1e-8)


def test_risk_parity_equalises_risk_contributions():
    rng = np.random.default_rng(3)
    r = rng.normal(size=(400, 4)) @ np.array(
        [[1, 0.4, 0, 0.1], [0, 1, 0.2, 0], [0, 0, 2, 0.3], [0, 0, 0, 0.5]]
    )
    w = om.risk_parity(np.cov(r, rowvar=False, ddof=1))
    rc, _ = risk_contributions(list(w), r.tolist())
    assert max(rc) - min(rc) < 1e-6


def test_hrp_two_assets_is_inverse_variance():
    cov = np.array([[0.04, 0.01], [0.01, 0.09]])
    w = om.hrp(cov)
    assert w[0] == pytest.approx((1 / 0.04) / (1 / 0.04 + 1 / 0.09))


def test_hrp_splits_two_clusters_first():
    # assets 0,1 move together and 2,3 move together; within each pair the variances are equal, so HRP gives each
    # pair half of its cluster's weight; the clusters split by inverse cluster variance (Lopez de Prado 2016)
    a, b = 0.04, 0.01
    cov = np.array([[a, 0.9 * a, 0, 0], [0.9 * a, a, 0, 0], [0, 0, b, 0.9 * b], [0, 0, 0.9 * b, b]])
    w = om.hrp(cov)
    v1 = 0.25 * (a + a + 2 * 0.9 * a)  # an equal-weighted pair's variance
    v2 = 0.25 * (b + b + 2 * 0.9 * b)
    alpha = 1 - v1 / (v1 + v2)
    assert w[0] == pytest.approx(alpha / 2) and w[1] == pytest.approx(alpha / 2)
    assert w[2] == pytest.approx((1 - alpha) / 2) and w[3] == pytest.approx((1 - alpha) / 2)


def test_weights_respect_bounds_for_every_method():
    cov = np.diag([0.01, 0.04, 0.09, 0.16])
    lo, hi = np.array([0.0, 0.0, 0.2, 0.0]), np.array([0.3, 1.0, 1.0, 1.0])
    for m in om.METHODS:
        w = om.optimise(m, cov, lo, hi)
        assert w.sum() == pytest.approx(1.0) and np.all(w >= lo - 1e-9) and np.all(w <= hi + 1e-9), m


def test_infeasible_bounds_raise():
    with pytest.raises(om.Infeasible):
        om.optimise("min_variance", np.eye(2), np.array([0.6, 0.6]), np.ones(2))
    with pytest.raises(om.Infeasible):
        om.optimise("hrp", np.eye(3), np.zeros(3), np.array([0.3, 0.3, 0.3]))


def test_mean_variance_is_not_offered():
    assert set(om.METHODS) == {"min_variance", "risk_parity", "hrp"}
    with pytest.raises(ValueError):
        om.optimise("mean_variance", np.eye(2), np.zeros(2), np.ones(2))


def test_annualised_vol_helper():
    assert om.ann_vol([0.01, -0.01, 0.01, -0.01]) == pytest.approx(
        np.std([0.01, -0.01] * 2, ddof=1) * math.sqrt(252)
    )


# --------------------------------------------------------------------------- walk-forward validation
def test_walk_forward_holds_weights_with_drift_between_rebalances():
    # train 2 days, test 2 days, fixed 50/50 target: day 1 of a window earns the 50/50 mix, day 2 the drifted mix
    r = np.array([[0, 0], [0, 0], [0.10, 0.0], [0.10, 0.0], [0.0, 0.0], [0.0, 0.0]])
    out = om.walk_forward(r, lambda _x: np.array([0.5, 0.5]), np.array([0.5, 0.5]), train=2, test=2)
    day1 = 0.5 * 0.10
    w_drift = 0.5 * 1.10 / (0.5 * 1.10 + 0.5)
    assert out.returns["method"][:2] == pytest.approx([day1, w_drift * 0.10])
    assert out.windows == 2
    # the second rebalance trades the drift back: one-way turnover ½Σ|w - w_drifted|
    w2 = 0.5 * 1.1 * 1.1 / (0.5 * 1.1 * 1.1 + 0.5)
    assert out.turnover_trades["method"] == pytest.approx([abs(0.5 - w2)])


def test_walk_forward_fits_only_on_the_past():
    seen = []
    r = np.arange(20, dtype=float).reshape(10, 2) / 1000
    om.walk_forward(
        r, lambda x: seen.append(x.copy()) or np.array([0.5, 0.5]), np.array([0.5, 0.5]), train=4, test=3
    )
    assert [s[-1, 0] for s in seen] == [r[3, 0], r[6, 0]]  # the window ends the day before the test period
    assert all(len(s) == 4 for s in seen)


def test_validation_passes_when_min_variance_avoids_a_volatile_asset():
    rng = np.random.default_rng(11)
    r = rng.normal(0, 0.01, size=(800, 3)) * np.array([1, 1, 3])
    cur = np.array([0.2, 0.2, 0.6])
    v = om.validate(r, "min_variance", np.zeros(3), np.ones(3), cur)
    assert v["passed"], v["reasons"]
    assert v["vol"]["method"] < v["vol"]["equal_weight"] < v["vol"]["current"]
    assert v["windows"] >= om.MIN_WINDOWS


def test_validation_needs_enough_windows():
    r = np.random.default_rng(2).normal(0, 0.01, size=(om.TRAIN + om.TEST * (om.MIN_WINDOWS - 1), 2))
    v = om.validate(r, "hrp", np.zeros(2), np.ones(2), np.array([0.5, 0.5]))
    assert not v["passed"]
    assert any("windows" in x for x in v["reasons"])


def test_validation_fails_when_the_method_is_riskier_than_equal_weight():
    # two uncorrelated assets of equal volatility: 1/N is the minimum-variance mix, so a forced 90/10 must fail
    r = np.random.default_rng(5).normal(0, 0.01, size=(700, 2))
    out = om.walk_forward(r, lambda _x: np.array([0.9, 0.1]), np.array([0.5, 0.5]))
    v = om.verdict(out)
    assert not v["passed"]
    assert any("equal weight" in x for x in v["reasons"])

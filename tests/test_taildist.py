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

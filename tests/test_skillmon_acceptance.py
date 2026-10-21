"""Issue #378's synthetic acceptance tests (a)-(c), with the registered constants of skillmon/specs.py. Seeds differ
from the ones the thresholds were derived with (378), so these are fresh paths, not the calibration sample.

(d) (the IPO 80 % interval's coverage in the 2019-2026 walk-forward) is not run here: the IPO quantile intervals are
registered (specs.LATER) but not wired in skill-monitor-v1."""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

from finresearch.skillmon import specs
from finresearch.skillmon import stats as st

C = specs.CALIBRATION
POOL = np.linspace(0.2, 0.8, 601)  # the registered reference mix


def _two_sided(seed: int, length: int, runs: int, **shift) -> np.ndarray:
    up = st.simulate_cummax(POOL, C.cusum_r_up, runs=runs, length=length, seed=seed, **shift)
    down = st.simulate_cummax(POOL, C.cusum_r_down, runs=runs, length=length, seed=seed, **shift)
    return np.minimum(st.run_lengths(up, C.cusum_h_up), st.run_lengths(down, C.cusum_h_down))


def test_a_in_control_run_length_is_within_20_percent_of_the_target() -> None:
    rl = _two_sided(seed=2026, length=5000, runs=1000)
    assert abs(rl.mean() / C.cusum_target_arl - 1) <= 0.20, rl.mean()


def test_a_fails_closed_with_a_wrong_threshold() -> None:
    up = st.simulate_cummax(POOL, C.cusum_r_up, runs=300, length=3000, seed=11)
    down = st.simulate_cummax(POOL, C.cusum_r_down, runs=300, length=3000, seed=11)
    rl = np.minimum(st.run_lengths(up, 2.0), st.run_lengths(down, 2.0))
    assert (
        rl.mean() < 0.8 * C.cusum_target_arl
    )  # h too low: false alarms far too soon, so the check above can fail


def test_b_a_planted_doubling_of_the_odds_is_found_within_100_events() -> None:
    for odds in (2.0, 0.5):
        rl = _two_sided(seed=4242, length=1500, runs=1000, shift_after=300, odds=odds)
        after = rl[rl > 300] - 300  # delay among paths with no false alarm before the shift
        assert len(after) > 300 and float(np.median(after)) < 100, (odds, np.median(after))


# --------------------------------------------------------------------------- (c) block-bootstrap coverage
H, N, BETA, GM, GB = 5, 500, 1.0, 0.8, 0.4  # outcome window, panel size, signal, model and benchmark slopes
K = BETA / math.sqrt(H)
PHI = np.vectorize(NormalDist().cdf)


def _true_bss() -> float:
    """1 - E(p_m - o)² / E(p_b - o)² with x ~ N(0, 1), P(o = 1 | x) = Φ(Kx): exact up to the quadrature."""
    x = np.linspace(-9, 9, 36001)
    w = np.exp(-(x**2) / 2)
    q, pm, pb = PHI(K * x), PHI(GM * K * x), PHI(GB * K * x)
    loss = lambda p: np.sum(w * (p**2 - 2 * p * q + q)) / np.sum(w)  # noqa: E731
    return float(1 - loss(pm) / loss(pb))


def _panel(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Overlapping outcomes: o_t = 1{βx_t + e_t+1 + ... + e_t+H > 0}, so neighbouring outcomes share H - 1 shocks."""
    x = rng.standard_normal(N)
    e = rng.standard_normal(N + H)
    s = np.convolve(e, np.ones(H), "valid")[1 : N + 1]
    o = (BETA * x + s > 0).astype(int)
    return PHI(GM * K * x), PHI(GB * K * x), o


def test_c_block_bootstrap_ci_covers_a_known_bss() -> None:
    true = _true_bss()
    assert 0.02 < true < 0.06  # a small, positive skill: the hard case for a promotion rule
    s = specs.SKILL
    hits = 0
    for i in range(1000):
        pm, pb, o = _panel(np.random.default_rng(10_000 + i))
        r = st.bss_ci(pm, pb, o, h=H, reps=s.reps, seed=i, level=s.level)
        hits += r["lo"] <= true <= r["hi"]
    assert hits / 1000 >= 0.93, hits / 1000

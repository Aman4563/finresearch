"""Strategy Lab overfitting statistics (#380): PBO by CSCV, White's Reality Check, Hansen's SPA, Romano-Wolf and
MinTRL. Expected values are hand-computed (worked in the comments) or computed by a separate, loop-based reading of
the paper in this file; never by calling the code under test."""

import itertools
import math
import statistics

import numpy as np
import pytest

from finresearch.stratlab import overfit as of


# --------------------------------------------------------------------------- MinTRL (Bailey & López de Prado 2012)
def test_min_trl_reproduces_the_issue_figures():
    # daily SR = annual / sqrt(252); z_0.05 one-sided = 1.6449; SR* = 0; kurtosis raw (3 = normal)
    # SR 1.0 normal: 1 + (1 + 0.5 * 0.0039683) * (1.6449 / 0.0629941)^2 = 1 + 1.0019841 * 681.84 = 684.2 days = 2.72 y
    sr = 1 / math.sqrt(252)
    assert of.min_trl(sr, 0.0, 3.0) == pytest.approx(684.2, abs=0.5)
    # skew -3, kurt 30: 1 + (1 + 3 * 0.0629941 + 29 / 4 * 0.0039683) * 681.84 = 1 + 1.21775 * 681.84 = 831.3 = 3.30 y
    assert of.min_trl(sr, -3.0, 30.0) == pytest.approx(831.3, abs=0.6)
    # SR 0.5 normal: (1.6449 / 0.0314970)^2 = 2727.4; * (1 + 0.5 * 0.000992) + 1 = 2729.7 days = 10.8 y
    assert of.min_trl(0.5 / math.sqrt(252), 0.0, 3.0) / 252 == pytest.approx(10.83, abs=0.02)


def test_min_trl_is_unknown_when_the_sharpe_does_not_beat_the_threshold():
    assert of.min_trl(0.0, 0.0, 3.0) is None
    assert of.min_trl(0.01, 0.0, 3.0, sr_star=0.02) is None
    assert of.min_trl(None, 0.0, 3.0) is None


# --------------------------------------------------------------------------- PBO by CSCV (Bailey et al. 2017)
def _blocks(per_block: list[list[float]]) -> np.ndarray:
    """One row per block (block length 1): T = S rows, N columns."""
    return np.array(per_block, dtype=float).T


def test_pbo_hand_example_where_every_in_sample_winner_loses_out_of_sample():
    # A = (4, 0, 1, 1), B = (0, 2, 2, 1) by block; statistic = mean; S = 4, so 6 halves:
    # train {1,2}: A 2.0 > B 1.0, test {3,4}: A 1.0 < B 1.5 -> rank 1 -> w = 1/3 -> lambda < 0
    # train {1,3}: A 2.5 > B 1.0, test {2,4}: A 0.5 < B 1.5;  train {1,4}: A 2.5 > B 0.5, test {2,3}: A 0.5 < B 2.0
    # train {2,3}: B 2.0 > A 0.5, test {1,4}: B 0.5 < A 2.5;  train {2,4}: B 1.5 > A 0.5, test {1,3}: B 1.0 < A 2.5
    # train {3,4}: B 1.5 > A 1.0, test {1,2}: B 1.0 < A 2.0  -> every half overfits: PBO = 1
    # (train, test) of the chosen one: (2,1) (2.5,.5) (2.5,.5) (2,.5) (1.5,1) (1.5,1); OLS slope = -0.5 / 1 = -0.5
    got = of.pbo_cscv(_blocks([[4, 0, 1, 1], [0, 2, 2, 1]]), s=4, stat="mean")
    assert got["combinations"] == 6
    assert got["pbo"] == pytest.approx(1.0)
    assert got["slope"] == pytest.approx(-0.5)
    assert got["prob_loss"] == pytest.approx(0.0)
    assert sorted(round(x, 6) for x in set(got["logits"])) == [round(math.log(0.5), 6)]


def test_pbo_is_zero_when_one_trial_dominates_every_block():
    got = of.pbo_cscv(_blocks([[5, 5, 5, 5], [0, 1, 0, 1]]), s=4, stat="mean")
    assert got["pbo"] == 0.0  # chosen A, test rank 2 -> w = 2/3 -> lambda = ln 2 > 0 in every half
    assert set(round(x, 6) for x in got["logits"]) == {round(math.log(2), 6)}


def test_pbo_counts_a_tied_median_rank_as_overfit():
    # A = (2, 0, 1, 1), B = (0, 1, 1, 1): train {1,2}: A 1.0 > B 0.5; test {3,4}: A 1 = B 1 -> average rank 1.5,
    # w = 1.5 / 3 = 0.5, lambda = 0, counted (the paper's PBO integrates f(lambda) over (-inf, 0])
    got = of.pbo_cscv(_blocks([[2, 0, 1, 1], [0, 1, 1, 1]]), s=4, stat="mean")
    assert 0.0 in [round(x, 12) for x in got["logits"]]


def _pbo_by_loops(m: np.ndarray, s: int) -> float:
    """The CSCV steps of Bailey et al. (2017) §2.2 read literally: Sharpe = mean / sample sd of the joined rows."""
    t, n = m.shape
    m = m[t % s :]
    size = len(m) // s
    blocks = [m[i * size : (i + 1) * size] for i in range(s)]
    over = combos = 0
    for c in itertools.combinations(range(s), s // 2):
        train = np.vstack([blocks[i] for i in c])
        test = np.vstack([blocks[i] for i in range(s) if i not in c])
        r = [statistics.mean(train[:, k]) / statistics.stdev(train[:, k]) for k in range(n)]
        rb = [statistics.mean(test[:, k]) / statistics.stdev(test[:, k]) for k in range(n)]
        best = r.index(max(r))
        rank = 1 + sum(x < rb[best] for x in rb) + 0.5 * (sum(x == rb[best] for x in rb) - 1)
        w = rank / (n + 1)
        over += math.log(w / (1 - w)) <= 0
        combos += 1
    return over / combos


def test_pbo_sharpe_matches_a_literal_loop_reading_of_the_paper():
    rng = np.random.default_rng(7)
    m = rng.normal(0, 1, size=(67, 5))  # 67 rows: the 3 oldest are trimmed to make 8 blocks of 8
    got = of.pbo_cscv(m, s=8)
    assert got["t_used"] == 64 and got["combinations"] == 70
    assert got["pbo"] == pytest.approx(_pbo_by_loops(m, 8))


def test_pbo_is_unknown_without_two_trials_or_enough_rows():
    assert of.pbo_cscv(np.zeros((40, 1)), s=4)["pbo"] is None
    assert (
        of.pbo_cscv(np.random.default_rng(0).normal(size=(3, 3)), s=4)["pbo"] is None
    )  # fewer rows than blocks
    assert of.pbo_cscv(np.random.default_rng(0).normal(size=(40, 3)), s=5)["pbo"] is None  # S must be even


def test_pbo_excludes_a_trial_whose_sharpe_is_undefined_and_says_so():
    rng = np.random.default_rng(1)
    m = rng.normal(0, 1, size=(32, 3))
    m[:, 1] = 0.0  # never traded: its Sharpe is undefined, never ranked as 0
    got = of.pbo_cscv(m, s=4)
    assert got["excluded"] == [1] and got["n_used"] == 2


# --------------------------------------------------------------------------- the stationary bootstrap (Politis & Romano)
def test_stationary_bootstrap_with_a_tiny_restart_probability_is_one_circular_block():
    idx = of.sb_indices(5, 50, mean_block=1e12, rng=np.random.default_rng(3))
    assert idx.shape == (50, 5)
    for row in idx:
        assert [int(x) for x in row] == [(int(row[0]) + t) % 5 for t in range(5)]  # wraps past the last day


def test_stationary_bootstrap_starts_new_blocks_at_rate_one_over_the_mean_block():
    n, b = 400, 400
    idx = of.sb_indices(n, b, mean_block=10, rng=np.random.default_rng(4))
    jumps = (idx[:, 1:] != (idx[:, :-1] + 1) % n).mean()
    # a new block starts with p = 0.1 and lands on the next day anyway with p = 1/n
    assert jumps == pytest.approx(0.1 * (1 - 1 / n), abs=0.004)


def test_kernel_variance_by_hand():
    # d = (1, 2, 3, 2): mean 2, deviations (-1, 0, 1, 0); gamma_0 = 2/4 = 0.5, gamma_1 = 0, gamma_2 = -1/4, gamma_3 = 0
    # q = 1/2: kappa(4, 2) = (2/4)(1/2)^2 + (2/4)(1/2)^2 = 0.25 -> omega^2 = 0.5 + 2 * 0.25 * (-0.25) = 0.375
    d = np.array([[1.0], [2.0], [3.0], [2.0]])
    assert of.kernel_var(d, q=0.5)[0] == pytest.approx(0.375)
    assert of.kernel_var(d, q=1.0)[0] == pytest.approx(0.5)  # q = 1: iid, only gamma_0


# Columns for the hand-worked tests (n = 4, sqrt(n) = 2), q = 1 so omega^2 = gamma_0:
# c1 = (1, 0, -1, 0): mean 0, omega = sqrt(0.5);  c2 = (1, 1, 0, 0): mean 0.5, omega = 0.5
# c3 = (-5, -5, -5, -4): mean -4.75, omega^2 = (3 * 0.0625 + 0.5625) / 4 = 0.1875, omega = 0.4330
C1, C2, C3 = [1, 0, -1, 0], [1, 1, 0, 0], [-5, -5, -5, -4]
ROWS = np.array([[0, 0, 0, 0], [2, 2, 2, 2], [0, 1, 2, 3], [1, 1, 0, 0], [3, 3, 3, 3]])


def test_reality_check_by_hand():
    # V = 2 * max(0, 0.5) = 1. Bootstrap max_k 2 (mean*_k - mean_k):
    # b1 means (1, 1) -> (2, 1) -> 2 > 1;  b2 (-1, 0) -> -1;  b3 (0, .5) -> 0;  b4 (.5, 1) -> (1, 1) -> 1, not > 1
    d = np.array([C1, C2], dtype=float).T
    got = of.reality_check(d, ROWS[:4])
    assert got["stat"] == pytest.approx(1.0) and got["p"] == pytest.approx(0.25)


def test_spa_by_hand_lower_consistent_and_upper():
    # T = max(0, 2*0/0.7071, 2*0.5/0.5, 2*(-4.75)/0.433) = 2. c3's t = -21.9 < -sqrt(2 ln ln 4) = -0.808, so the
    # consistent and lower statistics keep its negative mean; the upper one recentres it to 0.
    # Upper (Z* = mean* - mean): b1 (2.828, 2, -1.155) -> 2.828 > 2;  b2 (-2.83, -2, -1.15) -> 0;  b3 0;
    #   b4 (1.414, 2, -1.155) -> 2, not > 2;  b5 (0, -2, 2 * 0.75 / 0.433 = 3.46) -> 3.46 > 2:  p_u = 2/5
    # Consistent: c3's Z* = mean* (about -5 / 0.433 * 2 = -23): only b1 exceeds: p_c = 1/5; lower here = consistent
    d = np.array([C1, C2, C3], dtype=float).T
    got = of.spa(d, ROWS, q=1.0)
    assert got["stat"] == pytest.approx(2.0)
    assert got["p_upper"] == pytest.approx(0.4)
    assert got["p_consistent"] == pytest.approx(0.2)
    assert got["p_lower"] == pytest.approx(0.2)


def test_spa_is_unknown_for_a_constant_nonzero_trial():
    d = np.array([C1, [1, 1, 1, 1]], dtype=float).T
    assert of.spa(d, ROWS, q=1.0)["p_consistent"] is None


def test_romano_wolf_stepdown_by_hand():
    # c3' = (3, 3, 2, 2): mean 2.5, omega 0.5, t = 10; t1 = 0, t2 = 2.
    # Centred studentized bootstrap, rows b1-b4: b1 (2.828, 2, 2) max 2.828; b2 (-2.83, -2, -2) -2; b3 0;
    # b4 (1.414, 2, 2) max 2. The 0.75 quantile (linear) of (-2, 0, 2, 2.828): 2 + 0.25 * 0.828 = 2.207.
    # Step 1 rejects c3 (10 > 2.207), not c2 (2). Step 2 on {c1, c2}: the same maxima -> 2.207 -> nothing new.
    d = np.array([C1, C2, [3, 3, 2, 2]], dtype=float).T
    got = of.romano_wolf(d, ROWS[:4], q=1.0, alpha=0.25)
    assert got["rejected"] == [2]
    assert got["critical"] == pytest.approx([2.2071, 2.2071], abs=1e-4)

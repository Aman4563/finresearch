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
    m = m[t % s:]
    size = len(m) // s
    blocks = [m[i * size:(i + 1) * size] for i in range(s)]
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
    assert of.pbo_cscv(np.random.default_rng(0).normal(size=(3, 3)), s=4)["pbo"] is None  # fewer rows than blocks
    assert of.pbo_cscv(np.random.default_rng(0).normal(size=(40, 3)), s=5)["pbo"] is None  # S must be even


def test_pbo_excludes_a_trial_whose_sharpe_is_undefined_and_says_so():
    rng = np.random.default_rng(1)
    m = rng.normal(0, 1, size=(32, 3))
    m[:, 1] = 0.0  # never traded: its Sharpe is undefined, never ranked as 0
    got = of.pbo_cscv(m, s=4)
    assert got["excluded"] == [1] and got["n_used"] == 2

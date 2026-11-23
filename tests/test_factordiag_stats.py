"""The statistics of the stock factor diagnostics (#506) against small cases worked by hand: average ranks, Spearman
(with and without ties), quintiles, blocks, the Newey-West standard error, the block t interval, the two
randomisation nulls and Benjamini-Hochberg q-values. Unknown stays None."""

import math
import random

import numpy as np
import pytest

from finresearch.evals import stock_backtest as bt
from finresearch.evals import stock_universe as su
from finresearch.factordiag import stats as st


# --------------------------------------------------------------------------------------------- ranks and Spearman
def test_average_ranks_share_the_average_over_ties() -> None:
    assert st.average_ranks([10, 20, 20, 5]).tolist() == [2.0, 3.5, 3.5, 1.0]
    assert st.average_ranks([7, 7, 7]).tolist() == [2.0, 2.0, 2.0]  # (1 + 2 + 3) / 3
    assert st.average_ranks([]).tolist() == []


def test_average_ranks_agree_with_the_harness_rank_routine() -> None:
    rnd = random.Random(506)
    values = {
        f"S{i:02d}": float(rnd.choice([1, 2, 2, 3, 5, 8])) + rnd.choice([0.0, 0.0, 0.5]) for i in range(40)
    }
    theirs = su.ranks(values, descending=False)  # 1 = lowest, ties share the average rank
    mine = st.average_ranks(list(values.values()))
    assert mine.tolist() == [theirs[k] for k in values]


def test_spearman_by_hand_without_and_with_ties() -> None:
    # no ties: d = (1, -1, 1, -1, 0), sum d^2 = 4, rho = 1 - 6 x 4 / (5 x 24) = 0.8
    assert st.spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]) == pytest.approx(0.8, abs=1e-12)
    # ties: ranks x = (1, 2.5, 2.5, 4), y = (1, 3, 2, 4); deviations from 2.5: (-1.5, 0, 0, 1.5), (-1.5, .5, -.5, 1.5)
    # cov = 2.25 + 2.25 = 4.5, var x = 4.5, var y = 5, rho = 4.5 / sqrt(22.5) = sqrt(0.9)
    assert st.spearman([1, 2, 2, 3], [1, 3, 2, 4]) == pytest.approx(math.sqrt(0.9), abs=1e-12)
    assert st.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert st.spearman([1, 2, 3, 4], [1, 8, 27, 64]) == pytest.approx(
        1.0
    )  # any increasing map keeps the ranks


def test_spearman_is_unknown_when_a_side_is_constant_or_too_short() -> None:
    assert st.spearman([1, 1, 1], [1, 2, 3]) is None
    assert st.spearman([1, 2, 3], [4, 4, 4]) is None
    assert st.spearman([1], [2]) is None and st.spearman([], []) is None
    with pytest.raises(ValueError):
        st.spearman([1, 2], [1])


def test_quintiles_split_evenly_and_keep_tied_scores_together() -> None:
    assert st.quintiles(np.arange(10.0)).tolist() == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    # five tied lowest scores share rank 3: floor(5 x 2.5 / 10) + 1 = 2, so quintile 1 is EMPTY (the caller counts it);
    # ranks 6..10 give floor(2.75) + 1, floor(3.25) + 1, floor(3.75) + 1, floor(4.25) + 1, floor(4.75) + 1
    assert st.quintiles([1, 1, 1, 1, 1, 2, 3, 4, 5, 6]).tolist() == [2, 2, 2, 2, 2, 3, 4, 4, 5, 5]
    assert st.quintiles([3.0, 1.0, 2.0], q=3).tolist() == [3, 1, 2]


# --------------------------------------------------------------------------------------------- one series
def test_summary_by_hand_and_unknown_when_empty() -> None:
    s = st.summary(
        [0.1, -0.1, 0.3, 0.1]
    )  # mean 0.1; squared deviations 0, 0.04, 0.04, 0 -> sd = sqrt(0.08 / 3)
    sd = math.sqrt(0.08 / 3)
    assert s["n"] == 4 and s["mean"] == pytest.approx(0.1) and s["sd"] == pytest.approx(sd)
    assert s["icir"] == pytest.approx(0.1 / sd) and s["share_positive"] == 0.75
    assert st.summary([]) == {"n": 0, "mean": None, "sd": None, "icir": None, "share_positive": None}
    assert (
        st.summary([0.2])["sd"] is None and st.summary([0.2, 0.2])["icir"] is None
    )  # sd 0: no ratio, never inf


def test_newey_west_by_hand() -> None:
    # x = (1, 2, 4, 3, 5): mean 3, e = (-2, -1, 1, 0, 2); g0 = 10 / 5 = 2; g1 = (2 - 1 + 0 + 0) / 5 = 0.2; g2 = 0
    x = [1, 2, 4, 3, 5]
    one = st.newey_west(
        x, 1
    )  # Bartlett weight 1 - 1/2: LRV = 2 + 2 x 0.5 x 0.2 = 2.2; var of the mean = 0.44
    assert one["se"] == pytest.approx(math.sqrt(0.44)) and one["t"] == pytest.approx(3 / math.sqrt(0.44))
    two = st.newey_west(x, 2)  # weights 2/3 and 1/3: LRV = 2 + 2 x (2/3) x 0.2 + 0 = 34 / 15
    assert two["se"] == pytest.approx(math.sqrt(34 / 15 / 5))
    assert st.newey_west(x, 4) == {"lag": 4, "se": None, "t": None}  # fewer than lag + 2 values: unknown
    assert st.newey_west([2.0] * 10, 2)["t"] is None  # zero variance: unknown, never infinite


def test_newey_west_agrees_with_the_base_harness() -> None:
    x = np.random.default_rng(1).normal(0.02, 0.1, 150)
    assert st.newey_west(x, 6)["t"] == pytest.approx(bt.newey_west_t(list(x), lags=6), rel=1e-12)


def test_blocks_and_the_remainder_rule() -> None:
    assert st.blocks(30, 12) == [
        slice(0, 12),
        slice(12, 24),
        slice(24, 30),
    ]  # a remainder of half a block stands
    assert st.blocks(29, 12) == [slice(0, 12), slice(12, 29)]  # a shorter one joins the block before it
    assert st.blocks(24, 12) == [slice(0, 12), slice(12, 24)] and st.blocks(5, 12) == [slice(0, 5)]
    assert st.blocks(0, 12) == []
    with pytest.raises(ValueError):
        st.blocks(10, 0)


def test_block_interval_by_hand() -> None:
    # blocks of 2: means (2, 4, 6), sd 2, se = 2 / sqrt(3); t(2) at 97.5 % = 4.302653 and at 80 % = 1.060660 (tables)
    ci = st.block_interval([1, 3, 2, 6, 4, 8], 2)
    se = 2 / math.sqrt(3)
    assert ci["blocks"] == 3 and ci["se"] == pytest.approx(se)
    assert ci["lo"] == pytest.approx(4 - 4.302653 * se, abs=1e-5) and ci["hi"] == pytest.approx(
        4 + 4.302653 * se, abs=1e-5
    )
    assert ci["mde"] == pytest.approx((4.302653 + 1.060660) * se, abs=1e-5)
    few = st.block_interval([1, 3, 2, 6], 2)  # two blocks: below the registered minimum of three
    assert (
        few["blocks"] == 2
        and few["lo"] is None
        and few["hi"] is None
        and few["se"] is None
        and few["mde"] is None
    )


# --------------------------------------------------------------------------------------------- the two nulls
def test_sign_flip_p_by_hand() -> None:
    # block sums (1, 2, 3), total 6. The eight signed totals are +-6, +-4, +-2, +-0: only two reach 6 -> p = 2/8
    out = st.sign_flip_p([1, 0, 1, 1, 2, 1], 2, min_blocks=1)
    assert out == {"p": 0.25, "blocks": 3, "block": 2, "min_p": 0.25, "exact": True}
    # sums (3, -1, 2), total 4: |totals| are 4 (+++), 6 (+-+), 0 (++-), 2 (+--) and their mirrors -> four reach 4
    assert st.sign_flip_p([3, -1, 2], 1, min_blocks=1)["p"] == 0.5
    assert st.sign_flip_p([1, 1], 1, min_blocks=1)["p"] == 0.5  # ++ and -- reach 2; +- and -+ give 0
    assert st.sign_flip_p([0.1 + 0.2, 0.3], 1, min_blocks=1)["p"] == 0.5  # equal up to rounding still counts


def test_sign_flip_p_is_unknown_below_the_registered_blocks_and_counts_the_floor() -> None:
    four = st.sign_flip_p(np.ones(48), 12)  # four blocks: below MIN_BLOCKS_A = 5
    assert four["p"] is None and four["blocks"] == 4 and four["min_p"] is None
    six = st.sign_flip_p(np.ones(140), 24)  # 5 full blocks and a remainder of 20 (at least half): 6 blocks
    assert six["blocks"] == 6 and six["p"] == six["min_p"] == 2 / 64  # every block agrees: the floor, not 0


def test_sign_flip_monte_carlo_fallback_matches_the_exact_value() -> None:
    x = np.random.default_rng(3).normal(0.05, 0.2, 14 * 6)  # 14 blocks: 16,384 sign vectors
    exact = st.sign_flip_p(x, 6)
    mc = st.sign_flip_p(x, 6, exact_max=4, draws=20000, seed=11)
    assert exact["exact"] is True and mc["exact"] is False and mc["min_p"] == 1 / 20001
    assert mc["p"] == pytest.approx(
        exact["p"], abs=4 * math.sqrt(exact["p"] * (1 - exact["p"]) / 20000) + 1e-4
    )
    assert st.sign_flip_p(x, 6, exact_max=4, draws=500, seed=11) == st.sign_flip_p(
        x, 6, exact_max=4, draws=500, seed=11
    )


def test_period_permutation_p_by_hand() -> None:
    # block means (1, 2) against (3, 5): D = 1.5 - 4 = -2.5. Of the six ways to give two of the four blocks to period
    # one, the differences are 2.5 {1,2}, 1.5 {1,3}, 0.5 {1,5}, 0.5 {2,3}, 1.5 {2,5}, 2.5 {3,5}: two reach 2.5
    out = st.period_permutation_p([1, 2], [3, 5], 1, min_blocks=1)
    assert out["p"] == pytest.approx(2 / 6) and out["difference"] == -2.5 and out["blocks"] == [2, 2]
    assert out["min_p"] == pytest.approx(2 / 6) and out["exact"] is True
    # unequal sizes: (10,) against (1, 2, 3): D = 10 - 2 = 8; the four choices give 8, |1 - 5|, |2 - 14/3|, |3 - 13/3|
    one = st.period_permutation_p([10], [1, 2, 3], 1, min_blocks=1)
    assert one["p"] == 0.25 and one["min_p"] == 0.25  # only the observed split reaches 8: 1 of C(4, 1)


def test_period_permutation_p_is_unknown_below_the_registered_blocks() -> None:
    out = st.period_permutation_p(np.arange(36.0), np.arange(60.0), 12)  # 3 and 5 blocks: period one is short
    assert out["p"] is None and out["blocks"] == [3, 5] and out["difference"] == pytest.approx(17.5 - 29.5)
    empty = st.period_permutation_p([], [1.0, 2.0], 1)
    assert empty["p"] is None and empty["difference"] is None


def test_period_permutation_monte_carlo_fallback_matches_the_exact_value() -> None:
    rng = np.random.default_rng(5)
    a, b = rng.normal(0.08, 0.1, 72), rng.normal(0.0, 0.1, 72)
    exact = st.period_permutation_p(a, b, 12)
    mc = st.period_permutation_p(a, b, 12, exact_max=10, draws=20000, seed=7)
    assert exact["exact"] is True and exact["blocks"] == [6, 6] and exact["min_p"] == 2 / 924
    assert mc["exact"] is False
    assert mc["p"] == pytest.approx(
        exact["p"], abs=4 * math.sqrt(exact["p"] * (1 - exact["p"]) / 20000) + 1e-4
    )


# --------------------------------------------------------------------------------------------- Benjamini-Hochberg
def test_bh_qvalues_by_hand() -> None:
    # m = 4 with one unknown: sorted p = (0.01, 0.03, 0.04); m p / rank = 0.04, 0.06, 0.0533; running min from the top
    q = st.bh_qvalues({"a": 0.01, "b": 0.04, "c": 0.03, "d": None})
    assert (
        q["a"] == pytest.approx(0.04)
        and q["b"] == pytest.approx(0.16 / 3)
        and q["c"] == pytest.approx(0.16 / 3)
    )
    assert q["d"] is None and list(q) == ["a", "b", "c", "d"]
    # the textbook ladder p_i = 0.01 i with m = 5: every m p_(i) / i is 0.05
    assert list(st.bh_qvalues(dict(enumerate([0.01, 0.02, 0.03, 0.04, 0.05]))).values()) == pytest.approx(
        [0.05] * 5
    )
    assert list(st.bh_qvalues({"x": 0.9, "y": 0.95}).values()) == pytest.approx(
        [0.95, 0.95]
    )  # capped, monotone


def test_bh_family_size_is_the_registered_one_whatever_is_unknown() -> None:
    known = {"a": 0.004, "b": 0.03}
    assert st.bh_qvalues(known)["a"] == pytest.approx(0.008)  # m = 2
    q = st.bh_qvalues(known, m=20)  # the registered family of 20: 20 x 0.004 / 1 and 20 x 0.03 / 2
    assert q["a"] == pytest.approx(0.08) and q["b"] == pytest.approx(0.30)
    assert st.bh_qvalues({"a": 0.004, "b": None}, m=20) == {"a": pytest.approx(0.08), "b": None}
    assert st.bh_qvalues({"a": None}, m=20) == {"a": None} and st.bh_qvalues({}) == {}
    with pytest.raises(ValueError):
        st.bh_qvalues({"a": 0.1, "b": 0.2}, m=1)

"""The live skill monitor's statistics (#378, skillmon/stats.py): every value below is worked by hand from the formula
in the issue, and every function says "unknown" (None) where its statistic is undefined, never 0."""

from __future__ import annotations

import math

import pytest

from finresearch.skillmon import stats as st


def test_loss_differentials_and_bss() -> None:
    pm, pb, o = [0.8, 0.3], [0.5, 0.5], [1, 0]
    assert st.loss_diff(pm, pb, o) == pytest.approx([0.04 - 0.25, 0.09 - 0.25])
    assert st.bss(pm, pb, o) == pytest.approx(1 - 0.13 / 0.5)
    assert st.bss([], [], []) is None
    assert st.bss([1.0], [1.0], [1]) is None  # the benchmark is perfect: skill is undefined, never 0


def test_block_length_rule() -> None:
    assert st.block_len(500, 5) == 8  # 500^(1/3) = 7.94
    assert st.block_len(27, 1) == 3
    assert st.block_len(10, 5) == 5


def test_block_bootstrap_ci_is_seeded_and_brackets_the_estimate() -> None:
    import numpy as np

    rng = np.random.default_rng(1)
    pb = [0.5] * 200
    pm = list(rng.uniform(0.3, 0.7, 200))
    o = [int(rng.random() < p) for p in pm]
    a = st.bss_ci(pm, pb, o, h=3, reps=400, seed=7)
    assert a == st.bss_ci(pm, pb, o, h=3, reps=400, seed=7)
    assert a["block"] == 6 and a["n"] == 200 and a["lo"] < a["bss"] < a["hi"]
    assert st.bss_ci([], [], [], h=1, reps=10, seed=1)["bss"] is None


def test_calibration_in_the_large_and_spiegelhalter() -> None:
    assert st.citl([0.2, 0.4, 0.6], [1, 0, 1]) == pytest.approx(2 / 3 - 0.4)
    assert st.citl([], []) is None
    z = st.spiegelhalter([0.2, 0.5, 0.8], [1, 0, 1])
    assert z["z"] == pytest.approx(0.36 / math.sqrt(0.1152))
    assert z["p"] == pytest.approx(math.erfc(z["z"] / math.sqrt(2)))
    assert st.spiegelhalter([0.5, 0.5], [1, 0])["z"] is None  # zero variance: undefined


def test_bernoulli_cusum_path() -> None:
    w1 = math.log(2) - math.log(1.5)
    assert st.cusum_path([0.5, 0.5, 0.5], [1, 1, 0], 2.0) == pytest.approx(
        [w1, 2 * w1, max(0.0, 2 * w1 - math.log(1.5))]
    )
    r = st.two_sided_cusum([0.5] * 3, [1, 1, 1], h_up=0.5, h_down=9.0)
    assert r["alarm"] == {"side": "up", "at": 1}  # 0-based: S_up = 0.575 > 0.5 on the second event
    assert st.two_sided_cusum([0.5] * 3, [1, 1, 1], h_up=9.0, h_down=9.0)["alarm"] is None


def test_psi_pinball_conformal_and_pnl_cusum() -> None:
    assert st.psi([50, 50], [60, 40]) == pytest.approx(0.1 * math.log(1.2) + 0.1 * math.log(1.25))
    assert st.psi([0, 0], [1, 1]) is None
    assert st.pinball(10, 5, 0.1) == pytest.approx(4.5)
    assert st.pinball(10, 15, 0.1) == pytest.approx(0.5)
    assert st.aci_step(0.2, 0.2, 1, 0.005) == pytest.approx(0.196)
    assert st.pnl_cusum([5, 5, 20], mu0=10, k=1) == pytest.approx([4, 8, 0])

"""The model confidence set of the volatility race (Hansen, Lunde and Nason 2011, Econometrica 79, 453-497;
volrace/stats.py). The reference p-values come from arch 8.0's `arch.bootstrap.MCS(losses, size=0.10, reps=20000,
block_size=5, bootstrap="stationary", method="max", seed=390)`, run in a scratch environment outside the repo on the
generator below (verbatim); a bootstrap of 2,000 replicates here agrees within its Monte Carlo error."""

import numpy as np
import pytest

from finresearch.volrace import stats


def losses(n: int = 250, seed: int = 390) -> np.ndarray:
    rng = np.random.default_rng(seed)
    common = rng.gamma(2.0, 0.5, n)
    e = rng.normal(0, 0.3, (n, 4))
    return common[:, None] + np.array([0.0, 0.03, 0.06, 0.25]) + e


def test_mcs_p_values_match_the_arch_package() -> None:
    ref = {"m3": 0.0, "m2": 0.02455, "m1": 0.5659, "m0": 1.0}
    out = stats.mcs({f"m{i}": col.tolist() for i, col in enumerate(losses().T)}, alpha=0.10, reps=2000,
                    mean_block=5, seed=1)  # fmt: skip
    for k, p in ref.items():
        assert out.pvalues[k] == pytest.approx(p, abs=0.03), k
    assert out.included == ("m0", "m1") and set(out.excluded) == {"m2", "m3"}
    assert out.order == ("m3", "m2", "m1", "m0")  # eliminated first to last; the survivor has p = 1


def test_mcs_p_values_are_monotone_and_identical_models_are_all_kept() -> None:
    L = losses()
    out = stats.mcs(
        {f"m{i}": col.tolist() for i, col in enumerate(L.T)}, alpha=0.10, reps=500, mean_block=5, seed=2
    )
    ps = [out.pvalues[k] for k in out.order]
    assert ps == sorted(ps)  # an MCS p-value never falls along the elimination order
    same = stats.mcs(
        {"a": L[:, 0].tolist(), "b": L[:, 0].tolist()}, alpha=0.10, reps=200, mean_block=5, seed=3
    )
    assert same.included == ("a", "b") and same.pvalues == {"a": 1.0, "b": 1.0}


def test_mcs_refuses_unequal_or_unknown_losses() -> None:
    with pytest.raises(ValueError):
        stats.mcs({"a": [1.0, 2.0], "b": [1.0]}, alpha=0.1, reps=10, mean_block=1, seed=0)
    with pytest.raises(ValueError):
        stats.mcs({"a": [1.0, None], "b": [1.0, 2.0]}, alpha=0.1, reps=10, mean_block=1, seed=0)  # type: ignore[list-item]

"""stratlab/overfit (Reality Check, SPA, Romano-Wolf stepdown, kernel variance) and the three stationary-bootstrap
index functions against arch.bootstrap.

Conventions pinned first:
* arch takes LOSSES (lower is better) for a benchmark and the models; the module takes each trial's P&L over the
  benchmark "not trading". A zero benchmark loss and models = -P&L give arch the same differences d;
* `bootstrap="stationary"`, `block_size` = 1/q, `nested=False` (the kernel variance that matches the bootstrap);
* Monte Carlo noise is removed, not tolerated: arch's own resampled indices (same seed, same block) are read from
  its `StationaryBootstrap` and handed to the in-house functions, so equal algorithms must give EQUAL p-values.

Result: White's Reality Check is identical. The studentised SPA is not, and the whole difference is two choices:
* the module divides every bootstrap statistic by the SAMPLE's standard error omega_k; arch 9.0.0 re-estimates
  omega_k inside each bootstrap draw (a bootstrap-t);
* the module's statistic is max(max_k t_k, 0), floored at zero on both the sample and the bootstrap side; arch
  compares the unfloored maxima. This matters only when every trial's mean is negative ("all bad"), where the
  p-values differ most and both are far from rejecting.
Applying both of arch's choices on the test's side reproduces arch's three p-values exactly, so nothing else differs.
The module's two choices are what its docstring gives as Hansen (2005); the paper was not re-read for this
comparison, so which variant is the paper's own is not settled here.
"""

from __future__ import annotations

import math

import numpy as np
from arch.bootstrap import MCS, SPA, StationaryBootstrap, StepM

from finresearch.backtest import inference as bi
from finresearch.fincalc import taildist as td
from finresearch.stratlab import overfit as ov
from finresearch.volrace import stats as vr

T, K, BLOCK, REPS, SEED = 250, 6, 10, 400, 42
Q = 1 / BLOCK


def arch_indices(n: int, block: int, reps: int, seed: int) -> np.ndarray:
    """The (reps, n) indices arch's stationary bootstrap draws for this seed: the stream SPA and StepM use."""
    bs = StationaryBootstrap(block, np.arange(n), seed=seed)
    return np.stack([bs.index.copy() for _ in bs.bootstrap(reps)])


def designs() -> dict[str, np.ndarray]:
    """Synthetic daily P&L of K trials over T sessions (serially correlated noise, per-trial drifts)."""
    rng = np.random.default_rng(503)
    e = rng.standard_normal((T + 1, K)) * 0.01
    noise = e[1:] + 0.3 * e[:-1]  # MA(1): the block bootstrap has dependence to keep
    drift = {
        "all noise": np.zeros(K),
        "one good trial": np.r_[0.0030, np.zeros(K - 1)],
        "two good, one bad": np.r_[0.0035, 0.0028, -0.0040, np.zeros(K - 3)],
        "all bad": -0.002 * np.ones(K),
    }
    return {name: noise + mu for name, mu in drift.items()}


def bootstrap_t_pvalues(d: np.ndarray, idx: np.ndarray) -> list[float]:
    """arch 9's variant, rebuilt from the module's own pieces: each draw is studentised by ITS kernel variance,
    and neither the statistic nor the simulated maxima are floored at zero."""
    n = d.shape[0]
    mean = d.mean(axis=0)
    om = np.sqrt(ov.kernel_var(d, Q))
    t = math.sqrt(n) * mean / om
    stat = float(t.max())
    shifts = [np.maximum(mean, 0.0), np.where(t >= -math.sqrt(2 * math.log(math.log(n))), mean, 0.0), mean]
    sims = np.empty((len(idx), 3))
    for b, row in enumerate(idx):
        star = d[row]
        om_star = np.sqrt(ov.kernel_var(star, Q))
        for j, mu in enumerate(shifts):
            sims[b, j] = (math.sqrt(n) * (star.mean(axis=0) - mu) / om_star).max()
    return [float((sims[:, j] > stat).mean()) for j in range(3)]


def test_reality_check_and_spa_on_archs_own_resamples(oracle):
    idx = arch_indices(T, BLOCK, REPS, SEED)
    rc, spa_t, spa_h, n = 0.0, 0.0, (0.0, None), 0
    for name, d in designs().items():
        n += 1
        zero = np.zeros(T)
        white = SPA(zero, -d, block_size=BLOCK, reps=REPS, studentize=False, nested=False, seed=SEED)
        white.compute()
        rc = max(rc, abs(ov.reality_check(d, idx)["p"] - float(white.pvalues["upper"])))
        hansen = SPA(zero, -d, block_size=BLOCK, reps=REPS, studentize=True, nested=False, seed=SEED)
        hansen.compute()
        ref = [float(hansen.pvalues[k]) for k in ("lower", "consistent", "upper")]
        ours = ov.spa(d, idx, q=Q)
        got = [ours["p_lower"], ours["p_consistent"], ours["p_upper"]]
        assert got[0] <= got[1] <= got[2]
        spa_t = max(spa_t, max(abs(a - b) for a, b in zip(bootstrap_t_pvalues(d, idx), ref, strict=True)))
        gap = max(abs(a - b) for a, b in zip(got, ref, strict=True))
        spa_h = max(spa_h, (gap, (name, got, ref)), key=lambda x: x[0])
        # the unstudentised statistic and the gate's reading agree on every design
        assert (got[1] < 0.05) == (ref[1] < 0.05), (name, got, ref)
    oracle.check("overfit.reality_check (p-value)", "arch SPA(studentize=False), upper p-value, same resamples",
                 grid=n, max_diff=rc, limit=0.0, why="exactly equal: same indices, same statistic")  # fmt: skip
    oracle.check("overfit.kernel_var + per-draw studentising", "arch SPA(studentize=True) p-values, same resamples",
                 grid=3 * n, max_diff=spa_t, limit=0.0, why="exactly equal once arch's two choices are applied")  # fmt: skip
    oracle.note("overfit.spa (lower, consistent, upper p)", "arch SPA(studentize=True), same resamples", grid=3 * n,
                max_diff=spa_h[0], tolerance=f"none: largest p-value difference (design '{spa_h[1][0]}'); the 5 % "
                "reading agrees on all four designs",
                verdict="(b) convention: in-house uses the sample's omega and floors the statistic at 0 (its reading of Hansen 2005); arch 9 does neither")  # fmt: skip


def test_stepdown_rejections_against_arch_stepm(oracle):
    """Romano-Wolf's stepdown against arch's StepM at 5 %. arch builds StepM on its SPA (so the bootstrap-t above,
    and Hansen's consistent re-centring instead of Romano and Wolf's plain d* - d), so its critical values are not
    the module's; on designs whose trials are clearly good or clearly not, the rejected sets must still be equal."""
    idx = arch_indices(T, BLOCK, REPS, SEED)
    differ, n, rejected = 0, 0, 0
    for d in designs().values():
        n += 1
        step = StepM(
            np.zeros(T), -d, size=0.05, block_size=BLOCK, reps=REPS, studentize=True, nested=False, seed=SEED
        )
        step.compute()
        ours = ov.romano_wolf(d, idx, q=Q, alpha=0.05)
        differ += sorted(step.superior_models) != ours["rejected"]
        rejected += len(ours["rejected"])
    assert rejected >= 3  # the good trials are found, so equal sets are not equal by being empty
    oracle.check("overfit.romano_wolf (rejected set)", "arch StepM(size=0.05), same resamples", grid=n,
                 max_diff=differ, limit=0, why="designs with a different rejected set (clear-cut designs)",
                 verdict="agree on the sets; (b) arch's critical values use its SPA's bootstrap-t and re-centring")  # fmt: skip


def _continuation(idx: np.ndarray, n: int) -> tuple[float, int]:
    """Share of steps that follow the previous index (wrapping n-1 -> 0), over a (draws, n) index array."""
    follow = (idx[:, 1:] - idx[:, :-1]) % n == 1
    return float(follow.mean()), follow.size


def test_stationary_bootstrap_indices_have_archs_distribution(oracle):
    """Random streams differ, so distributions are compared: a step follows the previous index with probability
    (1 - 1/L) + (1/L)/n (a new block may start on the next index by chance), and the start of a block is uniform.
    Tolerance: 4 binomial standard errors on about 1.2 million steps per function, the same for arch itself."""
    n, block, draws = 200, 8, 6000
    expected = (1 - 1 / block) + (1 / block) / n
    samples = {
        "arch StationaryBootstrap": arch_indices(n, block, draws, 1),
        "taildist.stationary_bootstrap": np.stack(
            [td.stationary_bootstrap(n, n, block, np.random.default_rng(s)) for s in range(600)]
        ),
        "inference.stationary_indices": bi.stationary_indices(n, block, draws, np.random.default_rng(2)),
        "overfit.sb_indices": ov.sb_indices(n, draws, mean_block=block, rng=np.random.default_rng(3)),
    }
    worst, cells = 0.0, 0
    for name, idx in samples.items():
        share, steps = _continuation(idx, n)
        se = math.sqrt(expected * (1 - expected) / steps)
        worst = max(worst, abs(share - expected) / se)
        cells += steps
        assert abs(share - expected) < 4 * se, (name, share, expected, se)
        assert idx.min() == 0 and idx.max() == n - 1  # the whole circle is used, wrap-around included
        first = np.bincount(idx[:, 0], minlength=n) / len(idx)  # block starts are uniform on 0..n-1
        assert abs(first - 1 / n).max() < 5 * math.sqrt((1 / n) / len(idx)), name
    oracle.check("stationary bootstrap indices (taildist, inference, overfit)", "arch StationaryBootstrap + theory",
                 grid=cells, max_diff=worst, limit=4.0, why="binomial standard errors (Monte Carlo)")  # fmt: skip


def test_model_confidence_set_matches_arch_within_monte_carlo_error(oracle):
    """volrace/stats.mcs (T_max) against arch's MCS(method="max"). The module draws its own resamples, so here the
    Monte Carlo error cannot be removed: two independent bootstraps of B draws differ by up to
    4 sqrt(2 p (1 - p) / B) + 2/B in a p-value. The first design is the one tests/test_volrace_stats.py freezes from
    arch 8.0; arch 9.0.0 must still give those numbers (it does: its MCS did not change the way its SPA did)."""
    reps, worst, n = 4000, 0.0, 0
    for seed, bias in (
        (390, [0.0, 0.03, 0.06, 0.25]),
        (17, [0.0, 0.0, 0.10, 0.12, 0.30]),
        (5, [0.0, 0.01, 0.02]),
    ):
        rng = np.random.default_rng(seed)
        common = rng.gamma(2.0, 0.5, 250)
        losses = common[:, None] + np.array(bias) + rng.normal(0, 0.3, (250, len(bias)))
        ref = MCS(losses, size=0.10, reps=reps, block_size=5, method="max", bootstrap="stationary", seed=seed)
        ref.compute()
        theirs = {f"m{k}": float(v) for k, v in ref.pvalues["Pvalue"].items()}
        ours = vr.mcs(
            {f"m{i}": c.tolist() for i, c in enumerate(losses.T)}, alpha=0.10, reps=reps, mean_block=5, seed=1
        )
        for name, p in theirs.items():
            n += 1
            allowed = 4 * math.sqrt(2 * p * (1 - p) / reps) + 2 / reps
            worst = max(worst, abs(ours.pvalues[name] - p) / allowed)
        near = [
            m for m, p in theirs.items() if abs(p - 0.10) < 0.02
        ]  # a model on the 10 % line may fall either way
        assert set(ours.included) ^ {f"m{k}" for k in ref.included} <= set(near)
        if seed == 390:
            frozen = MCS(
                losses, size=0.10, reps=20000, block_size=5, method="max", bootstrap="stationary", seed=390
            )
            frozen.compute()
            got = [round(float(frozen.pvalues["Pvalue"][k]), 5) for k in (3, 2, 1, 0)]
            assert got == [
                0.0,
                0.02455,
                0.5659,
                1.0,
            ]  # the arch 8.0 values pasted in tests/test_volrace_stats.py
    oracle.check("volrace.stats.mcs (MCS p-values)", "arch MCS(method='max'), independent resamples", grid=n,
                 max_diff=worst, limit=1.0, why="x 4 Monte Carlo standard errors of a p-value difference")  # fmt: skip

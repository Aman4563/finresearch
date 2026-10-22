"""The model confidence set (Hansen, Lunde and Nason 2011, Econometrica 79, 453-497), T_max statistic.

For the surviving set M with mean losses Lbar_i, d_i = Lbar_i - mean_{j in M} Lbar_j, its bootstrap counterpart d*_bi
from the same resampled periods, var_i = mean_b (d*_bi - d_i)^2, t_i = d_i / sqrt(var_i), T = max_i t_i and
T*_b = max_i (d*_bi - d_i) / sqrt(var_i); p = share of T*_b > T. The model with the largest t_i is eliminated while
p < alpha; its MCS p-value is the running maximum of the p's so far, and the last survivor's is 1. The set at level
1 - alpha holds the models with MCS p >= alpha. The same resampled periods (Politis and Romano's stationary bootstrap,
fincalc.taildist.stationary_bootstrap) serve every step, as in arch's implementation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from finresearch.fincalc.taildist import stationary_bootstrap


@dataclass(frozen=True)
class Mcs:
    pvalues: dict[str, float]
    order: tuple[str, ...]  # eliminated first ... the last survivor
    included: tuple[str, ...]  # MCS p >= alpha, in input order
    excluded: tuple[str, ...]
    n: int


def mcs(
    losses: Mapping[str, Sequence[float]], *, alpha: float, reps: int, mean_block: float, seed: int
) -> Mcs:
    """The (1 - alpha) model confidence set of equally long, fully known loss series (ValueError otherwise)."""
    names = list(losses)
    lens = {len(v) for v in losses.values()}
    if len(lens) != 1 or any(x is None for v in losses.values() for x in v):
        raise ValueError("every model needs a known loss on the same periods")
    L = np.asarray([losses[k] for k in names], float).T  # n x m
    n = L.shape[0]
    rng = np.random.default_rng(seed)
    idx = np.stack([stationary_bootstrap(n, n, mean_block, rng) for _ in range(reps)])  # B x n
    lbar = L.mean(axis=0)
    lboot = np.column_stack([L[:, j][idx].mean(axis=1) for j in range(L.shape[1])])  # B x m
    alive = list(range(len(names)))
    pv: dict[str, float] = {}
    order: list[str] = []
    running = 0.0
    while len(alive) > 1:
        d = lbar[alive] - lbar[alive].mean()
        db = lboot[:, alive] - lboot[:, alive].mean(axis=1, keepdims=True)
        var = ((db - d) ** 2).mean(axis=0)
        if np.all(var <= 0):  # identical losses: nothing can be told apart
            break
        sd = np.where(var > 0, np.sqrt(np.where(var > 0, var, 1.0)), np.inf)
        t = d / sd
        tb = ((db - d) / sd).max(axis=1)
        p = float((tb > t.max()).mean())
        running = max(running, p)
        worst = alive[int(np.argmax(t))]
        pv[names[worst]] = running
        order.append(names[worst])
        alive.remove(worst)
    for i in alive:
        pv[names[i]] = 1.0
        order.append(names[i])
    included = tuple(k for k in names if pv[k] >= alpha)
    return Mcs(pv, tuple(order), included, tuple(k for k in names if k not in included), n)

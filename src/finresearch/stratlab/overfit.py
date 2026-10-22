"""Overfitting and data-snooping statistics for the Strategy Lab (#380, P2.5). Pure numpy; each returns None where its
statistic is undefined (too few trials or rows, zero variance), and a caller reads None as "fails" (unknown is never a
pass). The trial matrix is T × N: one row per session on a common day grid, one column per logged trial, holding that
trial's daily P&L (stratlab.trials builds it; a day a trial did not cover is never filled with 0).

Sources, with the equation each function follows:
- PBO: Bailey, D. H., Borwein, J. M., López de Prado, M. & Zhu, Q. J. (2017), The Probability of Backtest Overfitting,
  Journal of Computational Finance 20(4), 39-69 (revised version of 27-Feb-2015, §2.2 steps 1-5 and §3.1-3.2).
- Stationary bootstrap: Politis, D. N. & Romano, J. P. (1994), The Stationary Bootstrap, JASA 89(428), 1303-1313.
- Reality Check: White, H. (2000), A Reality Check for Data Snooping, Econometrica 68(5), 1097-1126.
- SPA: Hansen, P. R. (2005), A Test for Superior Predictive Ability, JBES 23(4), 365-380.
- Stepdown: Romano, J. P. & Wolf, M. (2005), Stepwise Multiple Testing as Formalized Data Snooping, Econometrica 73(4).
- MinTRL: Bailey, D. H. & López de Prado, M. (2012), The Sharpe Ratio Efficient Frontier, Journal of Risk 15(2), eq. 11.
"""

from __future__ import annotations

import math
from itertools import combinations
from statistics import NormalDist
from typing import Any

import numpy as np

N01 = NormalDist()


# --------------------------------------------------------------------------- MinTRL
def min_trl(sr: float | None, skew: float | None, kurt: float | None, *, sr_star: float = 0.0,
            alpha: float = 0.05) -> float | None:  # fmt: skip
    """Minimum track record length in periods: 1 + [1 − γ₃·SR + (γ₄−1)/4·SR²]·(z_α / (SR − SR*))² (Bailey & López de
    Prado 2012, eq. 11). SR, γ₃ (skewness) and γ₄ (raw kurtosis, 3 = normal) per period, z_α one-sided. None when an
    input is unknown or SR ≤ SR* (no track record is long enough: never a pass)."""
    if sr is None or skew is None or kurt is None or sr <= sr_star:
        return None
    z = N01.inv_cdf(1 - alpha)
    return 1 + (1 - skew * sr + (kurt - 1) / 4 * sr * sr) * (z / (sr - sr_star)) ** 2


# --------------------------------------------------------------------------- PBO by CSCV
def pbo_cscv(m: Any, s: int = 16, stat: str = "sharpe") -> dict[str, Any]:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation (Bailey et al. 2017 §2.2).

    The rows are cut into `s` equal blocks (the oldest T mod s rows are dropped). For each of the C(s, s/2) halves J
    (training) and its complement J̄ (testing), n* = argmax of the statistic on J (the first on a tie); its relative
    rank in J̄ is ω̄ = r̄/(N + 1) with r̄ in 1..N (1 = worst; tied values share their average rank) and λ = ln(ω̄/(1−ω̄)).
    PBO = #{λ ≤ 0} / C(s, s/2) (the paper integrates f(λ) over (−∞, 0], so a tied median counts as overfit). Also the
    OLS slope of R̄_n* on R_n* (performance degradation, §3.2) and Prob[R̄_n* < 0] (probability of loss).
    `stat`: "sharpe" = mean / sample sd of the joined rows (per period), or "mean". A trial whose Sharpe is undefined
    in any half (zero variance: e.g. it never traded there) is excluded and listed, never ranked as 0."""
    x = np.asarray(m, dtype=float)
    t, n = x.shape if x.ndim == 2 else (0, 0)
    out: dict[str, Any] = {"pbo": None, "slope": None, "prob_loss": None, "logits": [], "s": s, "stat": stat,
                           "combinations": 0, "t_used": 0, "n_used": 0, "excluded": []}  # fmt: skip
    size = t // s if s >= 2 and s % 2 == 0 else 0
    if size * (s // 2) < 2 or n < 2 or not np.isfinite(x).all():
        return out
    x = x[t - size * s:]
    blocks = x.reshape(s, size, n)
    bsum, bsq = blocks.sum(axis=1), (blocks**2).sum(axis=1)  # s × N
    combos = list(combinations(range(s), s // 2))
    ind = np.zeros((len(combos), s))
    for i, c in enumerate(combos):
        ind[i, list(c)] = 1.0
    k = size * (s // 2)

    def perf(w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        sm, sq = w @ bsum, w @ bsq
        mean = sm / k
        if stat == "mean":
            return mean, np.ones_like(mean, dtype=bool)
        var = (sq - k * mean**2) / (k - 1)
        ok = var > 1e-12 * np.maximum(sq / k, 1e-300)
        return np.where(ok, mean / np.sqrt(np.where(ok, var, 1.0)), np.nan), ok

    r, ok_r = perf(ind)
    rb, ok_b = perf(1.0 - ind)
    keep = (ok_r & ok_b).all(axis=0)
    out["excluded"] = [int(j) for j in np.flatnonzero(~keep)]
    r, rb = r[:, keep], rb[:, keep]
    nk = int(keep.sum())
    out.update(t_used=size * s, n_used=nk, combinations=len(combos))
    if nk < 2:
        return out
    best = np.argmax(r, axis=1)
    rows = np.arange(len(combos))
    v_is, v = r[rows, best], rb[rows, best]
    less = (rb < v[:, None]).sum(axis=1)
    eq = (rb == v[:, None]).sum(axis=1)
    w = (1 + less + 0.5 * (eq - 1)) / (nk + 1)
    lam = np.log(w / (1 - w))
    out["logits"] = [float(z) for z in lam]
    out["pbo"] = float((lam <= 0).mean())
    out["prob_loss"] = float((v < 0).mean())
    dx = v_is - v_is.mean()
    sxx = float((dx**2).sum())
    out["slope"] = float((dx * (v - v.mean())).sum() / sxx) if sxx > 0 else None
    return out

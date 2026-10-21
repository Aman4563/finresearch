"""Portfolio optimiser arithmetic (#346): pure numpy, no database, no network, floats like analytics_math.

Robust methods only. No method uses expected returns (unconstrained mean-variance amplifies estimation error in the
means, Michaud 1989; DeMiguel, Garlappi & Uppal 2009 found none of 14 mean-variance variants beat 1/N out of sample):
- covariance: Ledoit & Wolf (2004), "A well-conditioned estimator for large-dimensional covariance matrices", J.
  Multivariate Analysis 88: S* = δ·μI + (1-δ)·S with S the sample covariance (1/n, demeaned), μ = tr(S)/p,
  d² = ‖S - μI‖²/p, b̄² = (1/n²)·Σ_k ‖x_k x_kᵀ - S‖²/p, δ = min(b̄², d²)/d² (‖·‖ Frobenius; eqs. 14-15 of the paper,
  the form scikit-learn's LedoitWolf uses);
- min_variance: min wᵀΣw subject to Σw = 1 and lo ≤ w ≤ hi (long only), solved by accelerated projected gradient
  (FISTA, Beck & Teboulle 2009) with the Euclidean projection onto the capped simplex;
- risk_parity: equal risk contributions w_i(Σw)_i = wᵀΣw / n (Maillard, Roncalli & Teiletche 2010), by cyclical
  coordinate descent on ½yᵀΣy - (1/n)Σ log y_i (Griveau-Billion, Richard & Roncalli 2013), then w = y / Σy;
- hrp: hierarchical risk parity (López de Prado 2016, "Building diversified portfolios that outperform out of
  sample", J. Portfolio Management 42(4)): correlation distance d_ij = √(½(1-ρ_ij)), single-linkage clustering on the
  Euclidean distance between columns of d, quasi-diagonal leaf order, recursive bisection with inverse-variance
  cluster variances.
Risk parity and HRP have no bounds of their own: their weights are projected onto the bounds (the closest weights in
the Euclidean sense that satisfy them), so a binding bound changes the risk split, and the caller reports the weights'
actual risk contributions. Bounds that cannot add up to 1 raise `Infeasible`, never a silently relaxed answer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from finresearch.portfolio.analytics_math import TRADING_DAYS
from finresearch.portfolio.analytics_math import annualised_vol as ann_vol

METHODS = ("min_variance", "risk_parity", "hrp")
TOL = 1e-12
__all__ = [
    "METHODS",
    "Infeasible",
    "ann_vol",
    "hrp",
    "ledoit_wolf",
    "min_variance",
    "optimise",
    "risk_parity",
]


class Infeasible(ValueError):
    """The bounds admit no weights that add up to 1."""


def ledoit_wolf(x: np.ndarray) -> tuple[np.ndarray, float]:
    """(shrunk covariance, shrinkage δ in [0, 1]) of a T x N matrix of returns."""
    x = np.asarray(x, dtype=float)
    n, p = x.shape
    xc = x - x.mean(axis=0)
    s = xc.T @ xc / n
    mu = np.trace(s) / p
    d2 = float(np.sum((s - mu * np.eye(p)) ** 2)) / p
    if d2 <= 0:
        return s, 0.0
    norms = np.sum(xc**2, axis=1)
    # Σ_k ‖x_k x_kᵀ - S‖² = Σ_k ‖x_k‖⁴ - n‖S‖² (since Σ_k x_kᵀ S x_k = n·tr(S²))
    b2 = (float(np.sum(norms**2)) - n * float(np.sum(s**2))) / n**2 / p
    delta = min(b2, d2) / d2
    return delta * mu * np.eye(p) + (1 - delta) * s, delta


def check_bounds(lo: np.ndarray, hi: np.ndarray) -> None:
    if np.any(lo > hi + TOL) or lo.sum() > 1 + 1e-9 or hi.sum() < 1 - 1e-9:
        raise Infeasible(
            f"the bounds cannot add up to 100 % (lower bounds sum to {lo.sum():.2%}, upper to {hi.sum():.2%})"
        )


def project_capped_simplex(v: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """The closest w to v (Euclidean) with Σw = 1 and lo ≤ w ≤ hi: w = clip(v - τ, lo, hi), τ by bisection."""
    v, lo, hi = (np.asarray(a, dtype=float) for a in (v, lo, hi))
    check_bounds(lo, hi)
    a, b = float(np.min(v - hi)) - 1.0, float(np.max(v - lo)) + 1.0  # Σ at a ≥ 1 ≥ Σ at b
    for _ in range(200):
        t = (a + b) / 2
        if np.clip(v - t, lo, hi).sum() > 1:
            a = t
        else:
            b = t
    return np.clip(v - (a + b) / 2, lo, hi)


def min_variance(cov: np.ndarray, lo: np.ndarray, hi: np.ndarray, iters: int = 200_000) -> np.ndarray:
    cov = np.asarray(cov, dtype=float)
    step = 1 / (2 * float(np.max(np.linalg.eigvalsh(cov))))
    w = project_capped_simplex(np.full(len(cov), 1 / len(cov)), lo, hi)
    y, t = w.copy(), 1.0
    for _ in range(iters):
        w_new = project_capped_simplex(y - step * 2 * cov @ y, lo, hi)
        t_new = (1 + np.sqrt(1 + 4 * t * t)) / 2
        y = w_new + (t - 1) / t_new * (w_new - w)
        done = float(np.max(np.abs(w_new - w))) < 1e-13
        w, t = w_new, t_new
        if done:
            break
    return w


def risk_parity(cov: np.ndarray, iters: int = 10_000) -> np.ndarray:
    cov = np.asarray(cov, dtype=float)
    n = len(cov)
    y = 1 / np.sqrt(np.diag(cov))
    for _ in range(iters):
        prev = y.copy()
        for i in range(n):
            c = float(cov[i] @ y - cov[i, i] * y[i])
            y[i] = (-c + np.sqrt(c * c + 4 * cov[i, i] / n)) / (2 * cov[i, i])
        if float(np.max(np.abs(y - prev) / y)) < 1e-14:
            break
    return y / y.sum()


def _leaf_order(cov: np.ndarray) -> list[int]:
    sd = np.sqrt(np.diag(cov))
    corr = np.clip(cov / np.outer(sd, sd), -1, 1)
    d = np.sqrt(0.5 * (1 - corr))
    dd = np.sqrt(np.sum((d[:, None, :] - d[None, :, :]) ** 2, axis=2))
    clusters: dict[int, list[int]] = {i: [i] for i in range(len(cov))}
    nxt = len(cov)
    while len(clusters) > 1:
        ids = sorted(clusters)
        _, a, b = min(
            (float(np.min(dd[np.ix_(clusters[a], clusters[b])])), a, b)
            for i, a in enumerate(ids)
            for b in ids[i + 1 :]
        )
        clusters[nxt] = clusters.pop(a) + clusters.pop(b)
        nxt += 1
    return next(iter(clusters.values()))


def _cluster_var(cov: np.ndarray, idx: Sequence[int]) -> float:
    sub = cov[np.ix_(idx, idx)]
    ivp = 1 / np.diag(sub)
    ivp /= ivp.sum()
    return float(ivp @ sub @ ivp)


def hrp(cov: np.ndarray) -> np.ndarray:
    cov = np.asarray(cov, dtype=float)
    w = np.ones(len(cov))
    stack = [_leaf_order(cov)]
    while stack:
        items = stack.pop()
        if len(items) < 2:
            continue
        left, right = items[: len(items) // 2], items[len(items) // 2 :]
        vl, vr = _cluster_var(cov, left), _cluster_var(cov, right)
        alpha = 1 - vl / (vl + vr)
        w[left] *= alpha
        w[right] *= 1 - alpha
        stack += [left, right]
    return w / w.sum()


def optimise(method: str, cov: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Weights by `method` (one of METHODS) within lo ≤ w ≤ hi, Σw = 1."""
    lo, hi = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}: one of {', '.join(METHODS)}")
    check_bounds(lo, hi)
    if method == "min_variance":
        return min_variance(cov, lo, hi)
    raw = risk_parity(cov) if method == "risk_parity" else hrp(cov)
    return project_capped_simplex(raw, lo, hi)


# --------------------------------------------------------------------------- out-of-sample validation
# Pre-registered before any output on real holdings was looked at (#346, 22-Oct-2026). Changing one is a new
# registration: say so in the PR and keep the old values in the comment.
TRAIN = 252  # one year of daily returns to estimate the covariance
TEST = 21  # rebalance monthly; each month is held with drift and never seen by its own fit
MIN_WINDOWS = 6  # at least six out-of-sample months
MAX_TURNOVER = 1.0  # at most 100 % one-way turnover a year after the first trade
# pass = enough windows, out-of-sample volatility no higher than both equal weight and the current weights (each
# rebalanced on the same dates), and turnover within the cap. A fail shows the comparison and no trade list.


@dataclass
class WalkForward:
    returns: dict[str, list[float]]  # daily out-of-sample returns: method, equal_weight, current
    turnover_trades: dict[str, list[float]]  # one-way turnover at each rebalance after the first
    windows: int
    days: int


def _hold(r: np.ndarray, w: np.ndarray) -> tuple[list[float], np.ndarray]:
    """Daily returns of weights `w` held without trading over `r`, and the drifted weights at the end."""
    v, out = w.astype(float).copy(), []
    for row in r:
        out.append(float(v @ row / v.sum()))
        v = v * (1 + row)
    return out, v / v.sum()


def walk_forward(r: np.ndarray, fit: Callable[[np.ndarray], np.ndarray], current: np.ndarray, *, train: int = TRAIN,
                 test: int = TEST) -> WalkForward:  # fmt: skip
    """Refit every `test` days on the `train` days before, hold the weights with drift until the next refit."""
    r = np.asarray(r, dtype=float)
    n = r.shape[1]
    fixed = {"equal_weight": np.full(n, 1 / n), "current": np.asarray(current, dtype=float)}
    rets: dict[str, list[float]] = {k: [] for k in ("method", *fixed)}
    trades: dict[str, list[float]] = {k: [] for k in rets}
    drifted: dict[str, np.ndarray | None] = dict.fromkeys(rets)
    windows = 0
    for t0 in range(train, len(r), test):
        target = {"method": np.asarray(fit(r[t0 - train : t0]), dtype=float), **fixed}
        for k, w in target.items():
            if (prev := drifted[k]) is not None:
                trades[k].append(float(np.abs(w - prev).sum() / 2))
            got, drifted[k] = _hold(r[t0 : t0 + test], w)
            rets[k] += got
        windows += 1
    return WalkForward(rets, trades, windows, len(rets["method"]))


def verdict(wf: WalkForward) -> dict[str, Any]:
    vol = {k: (ann_vol(v) if len(v) > 2 else None) for k, v in wf.returns.items()}
    years = wf.days / TRADING_DAYS
    turnover = {k: (sum(v) / years if years > 0 else None) for k, v in wf.turnover_trades.items()}
    reasons = []
    if wf.windows < MIN_WINDOWS:
        reasons.append(f"only {wf.windows} out-of-sample windows (needs {MIN_WINDOWS})")
    m = vol["method"]
    if m is None:
        reasons.append("no out-of-sample returns")
    else:
        for k, label in (("equal_weight", "equal weight"), ("current", "your current weights")):
            if vol[k] is not None and m > vol[k]:
                reasons.append(f"out-of-sample volatility {m:.2%} is above {label} ({vol[k]:.2%})")
    t = turnover["method"]
    if t is not None and t > MAX_TURNOVER:
        reasons.append(f"turnover {t:.0%} a year is above the {MAX_TURNOVER:.0%} cap")
    return {"passed": not reasons, "reasons": reasons, "windows": wf.windows, "days": wf.days, "vol": vol,
            "turnover": turnover, "train": TRAIN, "test": TEST, "min_windows": MIN_WINDOWS,
            "max_turnover": MAX_TURNOVER}  # fmt: skip


def validate(
    r: np.ndarray, method: str, lo: np.ndarray, hi: np.ndarray, current: np.ndarray
) -> dict[str, Any]:
    """The pre-registered out-of-sample check of `method` (Ledoit-Wolf covariance, the same bounds) on `r`."""
    return verdict(walk_forward(r, lambda x: optimise(method, ledoit_wolf(x)[0], lo, hi), current))

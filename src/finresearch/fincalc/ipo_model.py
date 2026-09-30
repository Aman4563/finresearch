"""Small, dependency-light statistical models for the IPO listing model (docs/dev/RESEARCH_ROADMAP.md §D.1, item 14).

numpy only (no statsmodels/scikit-learn in this project):

- `fit_logistic`: L2-penalised logistic regression by Newton–Raphson. The intercept is not penalised, so with no
  features it recovers logit(ȳ) exactly, and with one binary feature and no penalty the two group rates.
- `fit_quantile`: linear quantile regression (Koenker & Bassett 1978) by the Hunter & Lange (2000) MM algorithm:
  |r| is majorised by r²/(2|rₖ|+ε) + |rₖ|/2, so each step is a weighted least-squares solve
  β = (XᵀWX + λI′)⁻¹(XᵀWy + (2τ−1)Xᵀ1) with w = 1/(|rₖ|+ε). The check loss never increases.
- Scores: Brier, Brier skill against a reference, AUC (Mann–Whitney, ties count ½), pinball loss, reliability bins.

Features are standardised with the TRAINING mean and standard deviation (`Standardizer`); a column that is constant
in training becomes 0 (so an all-zero post-2022 dummy in early folds is harmless).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass
class Standardizer:
    mean: np.ndarray
    std: np.ndarray

    @classmethod
    def fit(cls, x: np.ndarray) -> Standardizer:
        mean = x.mean(axis=0)
        std = x.std(axis=0)
        std = np.where(std > 1e-12, std, 1.0)
        return cls(mean, std)

    def transform(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) / self.std


def _design(z: np.ndarray) -> np.ndarray:
    return np.hstack([np.ones((z.shape[0], 1)), z])


def sigmoid(t: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * t))  # numerically stable logistic


def fit_logistic(
    z: np.ndarray, y: np.ndarray, *, l2: float = 1.0, max_iter: int = 100, tol: float = 1e-10
) -> np.ndarray:
    """β (intercept first) minimising −Σ[y log p + (1−y) log(1−p)] + (l2/2)·Σβⱼ² over the non-intercept terms."""
    x = _design(np.asarray(z, dtype=float))
    y = np.asarray(y, dtype=float)
    k = x.shape[1]
    pen = np.full(k, l2, dtype=float)
    pen[0] = 0.0
    beta = np.zeros(k)
    ybar = min(max(y.mean(), 1e-6), 1 - 1e-6)
    beta[0] = np.log(ybar / (1 - ybar))
    for _ in range(max_iter):
        p = sigmoid(x @ beta)
        grad = x.T @ (p - y) + pen * beta
        w = p * (1 - p)
        hess = (x * w[:, None]).T @ x + np.diag(pen) + 1e-9 * np.eye(k)
        step = np.linalg.solve(hess, grad)
        beta -= step
        if np.max(np.abs(step)) < tol:
            break
    return beta


def predict_logistic(beta: np.ndarray, z: np.ndarray) -> np.ndarray:
    return sigmoid(_design(np.asarray(z, dtype=float)) @ beta)


def check_loss(r: np.ndarray, tau: float) -> np.ndarray:
    """Koenker's check function ρ_τ(r) = r·(τ − 1[r < 0])."""
    return r * (tau - (r < 0))


def fit_quantile(z: np.ndarray, y: np.ndarray, tau: float, *, l2: float = 0.0, max_iter: int = 500,
                 eps: float = 1e-7, tol: float = 1e-10) -> np.ndarray:  # fmt: skip
    """β (intercept first) minimising Σρ_τ(yᵢ − xᵢβ) + (l2/2)·Σβⱼ² over the slopes (the τ-th conditional quantile)."""
    if not 0 < tau < 1:
        raise ValueError("tau must be within (0, 1)")
    x = _design(np.asarray(z, dtype=float))
    y = np.asarray(y, dtype=float)
    k = x.shape[1]
    pen = np.full(k, l2, dtype=float)
    pen[0] = 0.0
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    ones = np.ones(len(y))
    for _ in range(max_iter):
        r = y - x @ beta
        w = 1.0 / (np.abs(r) + eps)
        lhs = (x * w[:, None]).T @ x + 2 * np.diag(pen) + 1e-12 * np.eye(k)
        rhs = (x * w[:, None]).T @ y + (2 * tau - 1) * (x.T @ ones)
        new = np.linalg.solve(lhs, rhs)
        if np.max(np.abs(new - beta)) < tol:
            beta = new
            break
        beta = new
    return beta


def predict_linear(beta: np.ndarray, z: np.ndarray) -> np.ndarray:
    return _design(np.asarray(z, dtype=float)) @ beta


# --- scores -------------------------------------------------------------------------------------


def brier(p: Sequence[float], o: Sequence[float]) -> float:
    """(1/N)·Σ(pᵢ − oᵢ)²."""
    p, o = np.asarray(p, dtype=float), np.asarray(o, dtype=float)
    return float(np.mean((p - o) ** 2))


def brier_skill(bs: float, bs_ref: float) -> float | None:
    """BSS = 1 − BS/BS_ref: > 0 beats the reference, 0 ties, < 0 is worse."""
    return None if bs_ref == 0 else 1 - bs / bs_ref


def auc(p: Sequence[float], o: Sequence[float]) -> float | None:
    """Area under the ROC curve as the Mann–Whitney probability that a random positive outranks a random negative
    (ties count ½). None without both classes."""
    p, o = np.asarray(p, dtype=float), np.asarray(o, dtype=bool)
    pos, neg = p[o], p[~o]
    if len(pos) == 0 or len(neg) == 0:
        return None
    greater = (pos[:, None] > neg[None, :]).sum()
    ties = (pos[:, None] == neg[None, :]).sum()
    return float((greater + 0.5 * ties) / (len(pos) * len(neg)))


def pinball(y: Sequence[float], q: Sequence[float], tau: float) -> float:
    """Mean check loss of quantile forecasts q for outcomes y."""
    r = np.asarray(y, dtype=float) - np.asarray(q, dtype=float)
    return float(np.mean(check_loss(r, tau)))


def reliability(p: Sequence[float], o: Sequence[float], bins: int = 5) -> list[dict[str, float]]:
    """Equal-count bins of forecasts: mean forecast vs observed frequency per bin."""
    p, o = np.asarray(p, dtype=float), np.asarray(o, dtype=float)
    if len(p) == 0:
        return []
    order = np.argsort(p, kind="stable")
    out = []
    for chunk in np.array_split(order, min(bins, len(p))):
        if len(chunk):
            out.append(
                {"n": len(chunk), "mean_p": float(p[chunk].mean()), "observed": float(o[chunk].mean())}
            )
    return out

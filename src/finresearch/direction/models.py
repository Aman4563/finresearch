"""The three pre-registered models (direction.specs): trailing base rate, market-implied lognormal, L2 logistic.

Implied lognormal: ln(S[t+h]/S[t]) ~ N((r − σ²/2)·τ, σ²·τ) with σ = IV30/100 and τ = h / 252 (dividend yield 0,
stated in specs). With m = (r − σ²/2)·τ / (σ√τ):  P(up) = Φ(m),  P(up_k) = Φ(m − K),  P(down_k) = Φ(−m − K).

Logistic: minimise (1/n) Σ [−y ln p − (1 − y) ln(1 − p)] + (α·h/2)·‖w‖² over z-scored features (the training rows'
mean and standard deviation; z clipped to ±z_clip; a missing value is 0, i.e. the training mean), intercept not
penalised, by Newton's method (the objective is strictly convex in w; the intercept's curvature comes from the data).
A feature missing on more than max_missing of the training rows, or constant on them, is left out of that fit.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

from finresearch.direction import specs

N01 = NormalDist()


def implied_p(iv_30: float | None, h: int, event: str, rate: float = specs.RATE) -> float | None:
    if iv_30 is None or iv_30 <= 0:
        return None
    s = iv_30 / 100
    tau = h / specs.YEAR_SESSIONS
    m = (rate - s * s / 2) * tau / (s * math.sqrt(tau))
    if event == "up":
        return N01.cdf(m)
    if event == "up_k":
        return N01.cdf(m - specs.K)
    if event == "down_k":
        return N01.cdf(-m - specs.K)
    raise ValueError(event)


def base_rate_p(known: Sequence[int], spec: specs.BaseRate = specs.BASE) -> float | None:
    """The share of events among the last `window` outcomes already known; None with fewer than min_labels."""
    w = list(known)[-spec.window :]
    return None if len(w) < spec.min_labels else sum(w) / len(w)


@dataclass
class LogitFit:
    names: list[str]  # the features used (others were dropped by the missing / constant rule)
    mean: list[float]
    sd: list[float]
    coef: list[float]  # per used feature, on the z scale
    intercept: float
    n: int
    dropped: dict[str, str]
    converged: bool

    def predict(self, x: dict[str, float | None]) -> float:
        z = self.intercept
        for name, m, s, c in zip(self.names, self.mean, self.sd, self.coef, strict=True):
            v = x.get(name)
            if v is None:
                continue
            z += c * max(-specs.LOGIT.z_clip, min(specs.LOGIT.z_clip, (v - m) / s))
        return 1 / (1 + math.exp(-z)) if z > -700 else 0.0

    def to_json(self) -> dict:
        return {"features": dict(zip(self.names, [round(c, 5) for c in self.coef], strict=True)),
                "intercept": round(self.intercept, 5), "n": self.n, "dropped": self.dropped,
                "converged": self.converged}  # fmt: skip


def design(rows: Sequence[dict[str, float | None]], names: Sequence[str], spec: specs.Logistic = specs.LOGIT
           ) -> tuple[np.ndarray, list[str], list[float], list[float], dict[str, str]]:  # fmt: skip
    used, means, sds, cols, dropped = [], [], [], [], {}
    n = len(rows)
    for name in names:
        vals = [r.get(name) for r in rows]
        have = [v for v in vals if v is not None]
        if n == 0 or (n - len(have)) / n > spec.max_missing:
            dropped[name] = f"missing on {n - len(have)} of {n} training rows"
            continue
        m = sum(have) / len(have)
        sd = math.sqrt(sum((v - m) ** 2 for v in have) / max(len(have) - 1, 1))
        if sd <= 1e-12:
            dropped[name] = "constant on the training rows"
            continue
        used.append(name)
        means.append(m)
        sds.append(sd)
        cols.append([0.0 if v is None else max(-spec.z_clip, min(spec.z_clip, (v - m) / sd)) for v in vals])
    x = np.array(cols, dtype=float).T if cols else np.zeros((n, 0))
    return x, used, means, sds, dropped


def fit_logit(rows: Sequence[dict[str, float | None]], y: Sequence[int], h: int, names: Sequence[str] | None = None,
              spec: specs.Logistic = specs.LOGIT, iters: int = 50) -> LogitFit:  # fmt: skip
    names = list(names or specs.FEATURES)
    x, used, means, sds, dropped = design(rows, names, spec)
    n, k = x.shape
    yy = np.asarray(y, dtype=float)
    a = np.hstack([np.ones((n, 1)), x])
    w = np.zeros(k + 1)
    pbar = min(max(yy.mean(), 1e-6), 1 - 1e-6) if n else 0.5
    w[0] = math.log(pbar / (1 - pbar))
    pen = np.full(k + 1, spec.alpha * h)
    pen[0] = 0.0
    converged = False
    for _ in range(iters):
        z = np.clip(a @ w, -35, 35)
        p = 1 / (1 + np.exp(-z))
        grad = a.T @ (p - yy) / n + pen * w
        hess = (a.T * (p * (1 - p))) @ a / n + np.diag(pen) + 1e-10 * np.eye(k + 1)
        step = np.linalg.solve(hess, grad)
        w -= step
        if float(np.max(np.abs(step))) < 1e-9:
            converged = True
            break
    return LogitFit(used, means, sds, [float(v) for v in w[1:]], float(w[0]), n, dropped, converged)

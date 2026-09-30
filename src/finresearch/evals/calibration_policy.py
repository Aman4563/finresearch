"""Tiered recalibration policy for probability forecasts (portfolio-insights research §2.6; roadmap §C.10).

How much recalibration a track record can bear depends on how many resolved, OUT-OF-SAMPLE forecasts it has. The
policy, fixed in advance (evals/experiments/calibration_policy/PREREG.md):

    effective n          tier        forecast shown
    < 50                 base_rate   the group's observed base rate p̄ only; the model's p is labelled "uncalibrated"
    50 – 99              shrink      p′ = w·p + (1 − w)·p̄ with w = n/(n + k), k = 75 (midpoint of the 50–100 range)
    100 – 999            platt       p′ = σ(a·logit p + b), fitted by maximum likelihood on Platt's smoothed targets
                                     t₊ = (N₊ + 1)/(N₊ + 2), t₋ = 1/(N₋ + 2) (Platt 1999; Niculescu-Mizil & Caruana
                                     2005 [31] on when Platt beats isotonic)
    ≥ 1000               isotonic    pool-adjacent-violators (PAV) step function, linear between block centres
                                     ([31]: isotonic overfits below roughly a thousand cases)

"Effective n" is the number of independent outcomes. Forecasts whose outcome windows overlap (a 12-month stock event
issued monthly) share most of their information, so the caller divides by the overlap (n/12 for monthly 12-month
windows), as the stock backtest's Wilson intervals already do.

Also here, because the IPO calibration experiment uses them: a one-parameter temperature scaling p′ = σ(logit p / T)
with T minimising log loss, and a Brier-optimal blend of two forecasts on a fixed λ grid.

Pure functions (numpy only), no I/O. Always fit on forecasts that were made before their outcome was known.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from finresearch.fincalc.ipo_model import fit_logistic, sigmoid

EPS = 1e-6
TIERS = (("base_rate", 0), ("shrink", 50), ("platt", 100), ("isotonic", 1000))
SHRINK_K = 75.0
LAMBDA_GRID = tuple(round(i / 20, 2) for i in range(21))  # 0, 0.05, …, 1


def logit(p: np.ndarray | Sequence[float]) -> np.ndarray:
    q = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(q / (1 - q))


def policy_tier(n_effective: float) -> str:
    """The tier for an effective sample size (see the module table)."""
    tier = "base_rate"
    for name, lo in TIERS:
        if n_effective >= lo:
            tier = name
    return tier


def effective_n(n: int, overlap: float = 1.0) -> float:
    """n independent-outcome equivalents: n / overlap (overlap = 12 for monthly 12-month windows)."""
    if overlap <= 0:
        raise ValueError("overlap must be positive")
    return n / overlap


# ---------------------------------------------------------------------------------------------- calibrators
def fit_platt(p: Sequence[float], y: Sequence[float], *, smoothed: bool = True) -> tuple[float, float]:
    """(a, b) of p′ = σ(a·logit p + b) by maximum likelihood; Platt's smoothed targets by default."""
    x = logit(p)
    y = np.asarray(y, dtype=float)
    if smoothed:
        n_pos, n_neg = float(y.sum()), float(len(y) - y.sum())
        y = np.where(y > 0.5, (n_pos + 1) / (n_pos + 2), 1 / (n_neg + 2))
    beta = fit_logistic(x.reshape(-1, 1), y, l2=0.0)
    return float(beta[1]), float(beta[0])


def apply_platt(p: Sequence[float], a: float, b: float) -> np.ndarray:
    return sigmoid(a * logit(p) + b)


def log_loss(p: Sequence[float], y: Sequence[float]) -> float:
    q = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))


def fit_temperature(p: Sequence[float], y: Sequence[float], lo: float = 0.2, hi: float = 5.0) -> float:
    """T minimising the log loss of σ(logit p / T), by golden-section search on ln T ∈ [ln lo, ln hi]
    (the loss is unimodal in T for a fixed sample). T > 1 softens over-confident forecasts."""
    x = logit(p)
    y = np.asarray(y, dtype=float)

    def loss(ln_t: float) -> float:
        return log_loss(sigmoid(x / math.exp(ln_t)), y)

    a, b = math.log(lo), math.log(hi)
    g = (math.sqrt(5) - 1) / 2
    c, d = b - g * (b - a), a + g * (b - a)
    for _ in range(80):
        if loss(c) < loss(d):
            b = d
        else:
            a = c
        c, d = b - g * (b - a), a + g * (b - a)
    return math.exp((a + b) / 2)


def apply_temperature(p: Sequence[float], t: float) -> np.ndarray:
    return sigmoid(logit(p) / t)


def fit_blend(p_a: Sequence[float], p_b: Sequence[float], y: Sequence[float],
              grid: Sequence[float] = LAMBDA_GRID) -> float:  # fmt: skip
    """λ on the grid minimising the Brier score of λ·p_a + (1 − λ)·p_b; ties go to the smaller λ (more weight on
    the reference forecast p_b)."""
    a, b, y = (np.asarray(v, dtype=float) for v in (p_a, p_b, y))
    best, best_bs = grid[0], math.inf
    for lam in grid:
        bs = float(np.mean((lam * a + (1 - lam) * b - y) ** 2))
        if bs < best_bs - 1e-15:
            best, best_bs = lam, bs
    return float(best)


def fit_isotonic(p: Sequence[float], y: Sequence[float]) -> tuple[list[float], list[float]]:
    """Pool-adjacent-violators: returns (x knots, fitted values), non-decreasing. Knots are the mean forecast of
    each pooled block; `apply_isotonic` interpolates linearly between them and holds the ends flat."""
    order = np.argsort(np.asarray(p, dtype=float), kind="stable")
    xs = np.asarray(p, dtype=float)[order]
    ys = np.asarray(y, dtype=float)[order]
    blocks: list[list[float]] = []  # [sum_y, sum_x, count]
    for xv, yv in zip(xs, ys, strict=True):
        blocks.append([yv, xv, 1.0])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][2] > blocks[-1][0] / blocks[-1][2]:
            sy, sx, c = blocks.pop()
            blocks[-1][0] += sy
            blocks[-1][1] += sx
            blocks[-1][2] += c
    return [b[1] / b[2] for b in blocks], [b[0] / b[2] for b in blocks]


def apply_isotonic(p: Sequence[float], knots: Sequence[float], values: Sequence[float]) -> np.ndarray:
    return np.interp(np.asarray(p, dtype=float), np.asarray(knots), np.asarray(values))


# ---------------------------------------------------------------------------------------------- the policy
@dataclass
class Calibrator:
    tier: str
    n: int
    n_effective: float
    base_rate: float | None
    params: dict[str, Any] = field(default_factory=dict)

    def apply(self, p: float | Sequence[float]) -> np.ndarray:
        arr = np.atleast_1d(np.asarray(p, dtype=float))
        if self.base_rate is None:
            return arr
        if self.tier == "base_rate":
            return np.full(arr.shape, self.base_rate)
        if self.tier == "shrink":
            w = self.params["w"]
            return w * arr + (1 - w) * self.base_rate
        if self.tier == "platt":
            return apply_platt(arr, self.params["a"], self.params["b"])
        return apply_isotonic(arr, self.params["knots"], self.params["values"])

    def to_json(self) -> dict[str, Any]:
        return {"tier": self.tier, "n": self.n, "n_effective": round(self.n_effective, 2),
                "base_rate": self.base_rate, "params": self.params, "description": describe(self.tier)}  # fmt: skip


def describe(tier: str) -> str:
    return {
        "base_rate": "fewer than 50 independent outcomes: show the base rate only; the model is uncalibrated",
        "shrink": f"50–99 outcomes: shrink toward the base rate, w = n/(n + {SHRINK_K:g})",
        "platt": "100–999 outcomes: Platt scaling on out-of-sample forecasts (smoothed targets)",
        "isotonic": "1,000+ outcomes: isotonic (PAV) recalibration",
    }[tier]


def fit_policy(
    p: Sequence[float], y: Sequence[float], *, overlap: float = 1.0, k: float = SHRINK_K
) -> Calibrator:
    """Fit the tier the track record allows. `p` must be forecasts made before their outcomes `y` (0/1)."""
    if len(p) != len(y):
        raise ValueError(f"{len(p)} forecasts but {len(y)} outcomes")
    n = len(p)
    n_eff = effective_n(n, overlap)
    tier = policy_tier(n_eff)
    base = float(np.mean(y)) if n else None
    params: dict[str, Any] = {}
    if tier == "shrink":
        params["w"] = n_eff / (n_eff + k)
    elif tier == "platt":
        a, b = fit_platt(p, y)
        params.update(a=a, b=b)
    elif tier == "isotonic":
        knots, values = fit_isotonic(p, y)
        params.update(knots=knots, values=values)
    return Calibrator(tier=tier, n=n, n_effective=n_eff, base_rate=base, params=params)


# overlapping outcome windows per ledger asset: a stock forecast can be logged every month for a 12-month event
OVERLAP = {"stock": 12.0}


def group_policy(asset: str, n_resolved: int) -> dict[str, Any]:
    """The tier a ledger group (asset, method) has reached, for `/api/calibration`. Informational: nothing applies a
    calibrator to displayed probabilities yet (evals/experiments/calibration_policy/PREREG.md)."""
    overlap = OVERLAP.get(asset, 1.0)
    n_eff = effective_n(n_resolved, overlap)
    tier = policy_tier(n_eff)
    nxt = next(((name, lo) for name, lo in TIERS if lo > n_eff), None)
    return {"tier": tier, "n": n_resolved, "n_effective": round(n_eff, 2), "overlap": overlap,
            "description": describe(tier),
            "next_tier": None if nxt is None else {"tier": nxt[0], "at_n": math.ceil(nxt[1] * overlap)}}  # fmt: skip

"""Forecast metrics for the direction model (#327). Pure functions; p = forecast probability, o = outcome (1/0).

- Brier = mean (p − o)²; log loss with p clipped to [1e-6, 1 − 1e-6] (evals.calibration).
- Skill against a reference forecast on the SAME days: 1 − Brier_model / Brier_ref (and the same for log loss).
- Hit rate at 0.5: the share of days where (p > 0.5) equals the outcome (a forecast of exactly 0.5 counts as "no").
- AUC: the Mann-Whitney probability that a random event day got a higher forecast than a random non-event day (ties
  count ½); None when either class is empty.
- Calibration slope and intercept: the unpenalised logistic regression o ~ a + b·logit(p) (Cox 1958). Perfect
  calibration is a = 0, b = 1; b < 1 means the forecasts are too extreme, b > 1 too timid.
- Diebold-Mariano (1995) on d_t = loss_ref,t − loss_model,t (positive = the model is better): DM = d̄ / √(V/n) with the
  Newey-West (Bartlett) long-run variance V over `lag` autocovariances; the Harvey, Leybourne & Newbold (1997)
  correction multiplies it by √((n + 1 − 2h + h(h − 1)/n) / n); the one-sided p-value is P(t_{n−1} > DM).
  `diebold_mariano_hln` (#381, not used by any registered test) puts the same factor on the rectangular variance
  with h − 1 lags that HLN derived it for.
- Holm (1979) step-down adjustment of a family of p-values.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from finresearch.evals import calibration as cal


def brier_each(p: Sequence[float], o: Sequence[int]) -> list[float]:
    return [(a - b) ** 2 for a, b in zip(p, o, strict=True)]


def logloss_each(p: Sequence[float], o: Sequence[int], eps: float = cal.EPS) -> list[float]:
    out = []
    for a, b in zip(p, o, strict=True):
        q = min(1 - eps, max(eps, a))
        out.append(-(b * math.log(q) + (1 - b) * math.log(1 - q)))
    return out


def mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def skill(model: float | None, ref: float | None) -> float | None:
    return None if model is None or ref is None or ref == 0 else 1 - model / ref


def hit_rate(p: Sequence[float], o: Sequence[int]) -> float | None:
    return mean([float((a > 0.5) == (b == 1)) for a, b in zip(p, o, strict=True)])


def auc(p: Sequence[float], o: Sequence[int]) -> float | None:
    pos = [a for a, b in zip(p, o, strict=True) if b == 1]
    neg = [a for a, b in zip(p, o, strict=True) if b == 0]
    if not pos or not neg:
        return None
    # rank-based Mann-Whitney U with average ranks for ties
    allp = sorted((v, i) for i, v in enumerate([*pos, *neg]))
    ranks = [0.0] * len(allp)
    i = 0
    while i < len(allp):
        j = i
        while j + 1 < len(allp) and allp[j + 1][0] == allp[i][0]:
            j += 1
        for m in range(i, j + 1):
            ranks[allp[m][1]] = (i + j) / 2 + 1
        i = j + 1
    r_pos = sum(ranks[: len(pos)])
    u = r_pos - len(pos) * (len(pos) + 1) / 2
    return u / (len(pos) * len(neg))


def calibration_fit(p: Sequence[float], o: Sequence[int], iters: int = 100) -> tuple[float, float] | None:
    """(intercept a, slope b) of o ~ a + b·logit(p); None when undefined (one class only, or constant forecasts)."""
    if len(p) < 3 or len(set(o)) < 2:
        return None
    x = np.array([math.log(min(1 - 1e-6, max(1e-6, v)) / (1 - min(1 - 1e-6, max(1e-6, v)))) for v in p])
    if float(np.std(x)) < 1e-9:
        return None
    y = np.asarray(o, dtype=float)
    a = np.column_stack([np.ones(len(x)), x])
    w = np.zeros(2)
    for _ in range(iters):
        q = 1 / (1 + np.exp(-np.clip(a @ w, -35, 35)))
        g = a.T @ (q - y)
        h = (a.T * (q * (1 - q))) @ a + 1e-9 * np.eye(2)
        step = np.linalg.solve(h, g)
        w -= step
        if float(np.max(np.abs(step))) < 1e-10:
            break
    if not np.all(np.isfinite(w)):
        return None
    return float(w[0]), float(w[1])


def newey_west_var(d: Sequence[float], lag: int, kernel: str = "bartlett") -> float:
    """Long-run variance of d, divided by n: the variance of its mean. `bartlett` (registered): weights 1 − j/(lag + 1);
    `rectangular` (#381): weight 1 for every lag up to `lag`, which can be negative."""
    if kernel not in ("bartlett", "rectangular"):
        raise ValueError(f"unknown kernel {kernel!r}")
    x = np.asarray(d, dtype=float)
    n = len(x)
    e = x - x.mean()
    v = float(e @ e) / n
    for j in range(1, min(lag, n - 1) + 1):
        w = 1 - j / (lag + 1) if kernel == "bartlett" else 1.0
        v += 2 * w * float(e[j:] @ e[:-j]) / n
    return v / n


def _t_sf(t: float, df: int) -> float:
    """P(T > t) for Student's t with df degrees of freedom (regularised incomplete beta via continued fraction)."""
    if df <= 0:
        return float("nan")
    x = df / (df + t * t)
    tail = 0.5 * _betainc(df / 2, 0.5, x)
    return tail if t >= 0 else 1 - tail


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbeta) * _cf(a, b, x) / a
    return 1 - math.exp(lbeta) * _cf(b, a, 1 - x) / b


def _cf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for num in (
            m * (b - m) * x / ((a + m2 - 1) * (a + m2)),
            -(a + m) * (a + b + m) * x / ((a + m2) * (a + m2 + 1)),
        ):
            d = 1 + num * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + num / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < 1e-14:
            break
    return h


def diebold_mariano(loss_ref: Sequence[float], loss_model: Sequence[float], h: int, lag: int,
                    kernel: str = "bartlett") -> dict[str, float | int | None]:  # fmt: skip
    """One-sided test that the model's expected loss is below the reference's (module docstring)."""
    d = [a - b for a, b in zip(loss_ref, loss_model, strict=True)]
    n = len(d)
    if n < 10:
        return {"n": n, "mean_diff": mean(d), "dm": None, "p": None, "lag": lag}
    v = newey_west_var(d, lag, kernel)
    if v <= 0:
        return {"n": n, "mean_diff": mean(d), "dm": None, "p": None, "lag": lag}
    dm = (sum(d) / n) / math.sqrt(v)
    corr = (n + 1 - 2 * h + h * (h - 1) / n) / n
    dm_hln = dm * math.sqrt(corr) if corr > 0 else dm
    return {"n": n, "mean_diff": sum(d) / n, "dm": dm_hln, "p": _t_sf(dm_hln, n - 1), "lag": lag}


def diebold_mariano_hln(loss_ref: Sequence[float], loss_model: Sequence[float], h: int
                        ) -> dict[str, float | int | str | None]:  # fmt: skip
    """The HLN-corrected DM test on the variance HLN derived their factor for (#381, P2.6): the rectangular
    long-run variance with h − 1 lags, n⁻¹(γ̂₀ + 2 Σ_{k=1}^{h−1} γ̂_k) (Diebold & Mariano 1995; Harvey, Leybourne &
    Newbold 1997). The registered `diebold_mariano` mixes Bartlett weights over max(h − 1, 5) lags with the same
    factor, which over-rejects a true null for overlapping h (evals/experiments/fno_backtest/ADDENDUM.md (c)).
    A variance that is not positive gives dm = p = None (unknown), never a p-value."""
    return {**diebold_mariano(loss_ref, loss_model, h, max(h - 1, 0), "rectangular"), "kernel": "rectangular"}


def holm(pvals: dict[str, float | None]) -> dict[str, float | None]:
    """Holm-adjusted p-values (monotone step-down); None stays None and does not count toward the family size."""
    known = sorted((p, k) for k, p in pvals.items() if p is not None)
    m = len(known)
    out: dict[str, float | None] = {k: None for k, p in pvals.items() if p is None}
    running = 0.0
    for i, (p, k) in enumerate(known):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def reliability(p: Sequence[float], o: Sequence[int], bins: int) -> list[dict]:
    return [{"n": b.n, "mean_p": round(b.mean_p, 4), "observed": round(b.observed, 4),
             "ci": [round(b.ci[0], 4), round(b.ci[1], 4)] if b.ci else None}
            for b in cal.reliability_bins(list(p), list(o), bins)]  # fmt: skip


def summary(p: Sequence[float], o: Sequence[int], bins: int = 10) -> dict:
    b, ll = brier_each(p, o), logloss_each(p, o)
    fit = calibration_fit(p, o)
    return {"n": len(p), "base_rate": mean([float(x) for x in o]), "mean_p": mean(list(p)), "brier": mean(b),
            "log_loss": mean(ll), "hit_rate": hit_rate(p, o), "auc": auc(p, o),
            "calibration_intercept": None if fit is None else fit[0],
            "calibration_slope": None if fit is None else fit[1], "reliability": reliability(p, o, bins)}  # fmt: skip

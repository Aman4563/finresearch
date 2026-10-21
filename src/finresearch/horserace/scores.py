"""Scores and tests of the card horse race (#377, registered in horserace/specs.py). Pure functions.

- Murphy (1973): BS = REL - RES + UNC with REL = Σ n_k (p̄_k - ō_k)² / N, RES = Σ n_k (ō_k - ō)² / N, UNC = ō(1 - ō)
  over equal-width bins; `residual` = BS - (REL - RES + UNC), the within-bin terms (Stephenson et al. 2008), zero
  only when each bin holds one forecast value.
- A P&L distribution is given as 20 quantile dots at (i + 0.5) / 20: q(u) is piecewise-linear through them, its end
  segments extended to u = 0 and 1 (so U(a, b)'s dots give back U(a, b) exactly).
- CRPS(F, y) = 2 ∫₀¹ (1{y < q(u)} - u)(q(u) - y) du (the quantile-score form of E|X - y| - ½E|X - X'|; Gneiting &
  Raftery 2007), midpoint rule on `nodes` points. PIT u = F(y), clipped to [clip, 1 - clip].
- VaR_α = q(α); ES_α = (1/α) ∫₀^α q(u) du, the SIGNED tail mean of the P&L (negative for a loss), exact.
- Kupiec (1995) POF: LR = -2 ln[(1-p)^(n-x) p^x] + 2 ln[(1 - x/n)^(n-x) (x/n)^x], χ²(1).
- Christoffersen (1998) independence: LR = -2 ln[(1-π)^(n00+n10) π^(n01+n11)]
  + 2 ln[(1-π01)^n00 π01^n01 (1-π11)^n10 π11^n11], χ²(1).
- χ² survival: 1 df erfc(√(x/2)); 2 df e^(-x/2); 3 df erfc(√(x/2)) + √(2x/π) e^(-x/2).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from itertools import pairwise

import numpy as np


def _xlogy(x: float, y: float) -> float:
    return 0.0 if x == 0 else x * math.log(y)


def chi2_sf(x: float, df: int) -> float:
    if x <= 0:
        return 1.0
    if df == 1:
        return math.erfc(math.sqrt(x / 2))
    if df == 2:
        return math.exp(-x / 2)
    if df == 3:
        return math.erfc(math.sqrt(x / 2)) + math.sqrt(2 * x / math.pi) * math.exp(-x / 2)
    raise ValueError(f"chi-square survival for {df} df is not implemented")


def murphy(p: Sequence[float], o: Sequence[int], bins: int = 10) -> dict[str, float]:
    n = len(p)
    if n == 0 or n != len(o):
        raise ValueError("murphy needs equal, non-empty forecast and outcome lists")
    groups: dict[int, list[tuple[float, int]]] = {}
    for a, b in zip(p, o, strict=True):
        groups.setdefault(min(int(a * bins), bins - 1), []).append((a, b))
    obar = sum(o) / n
    rel = sum(
        len(g) * (sum(a for a, _ in g) / len(g) - sum(b for _, b in g) / len(g)) ** 2 for g in groups.values()
    )
    res = sum(len(g) * (sum(b for _, b in g) / len(g) - obar) ** 2 for g in groups.values())
    bs = sum((a - b) ** 2 for a, b in zip(p, o, strict=True)) / n
    rel, res, unc = rel / n, res / n, obar * (1 - obar)
    return {"brier": bs, "rel": rel, "res": res, "unc": unc, "residual": bs - (rel - res + unc), "bins": bins}


def kupiec(x: int, n: int, p: float) -> dict[str, float | int]:
    if n <= 0:
        raise ValueError("kupiec needs n > 0")
    h = x / n
    lr = -2 * (_xlogy(n - x, 1 - p) + _xlogy(x, p)) + 2 * (_xlogy(n - x, 1 - h) + _xlogy(x, h))
    lr = max(lr, 0.0)
    return {"x": x, "n": n, "rate": h, "expected": p, "lr": lr, "p": chi2_sf(lr, 1)}


def christoffersen(hits: Sequence[int]) -> dict[str, float | int | None]:
    c = {(a, b): 0 for a in (0, 1) for b in (0, 1)}
    for a, b in pairwise(hits):
        c[(a, b)] += 1
    n00, n01, n10, n11 = c[(0, 0)], c[(0, 1)], c[(1, 0)], c[(1, 1)]
    out: dict[str, float | int | None] = {
        "n00": n00,
        "n01": n01,
        "n10": n10,
        "n11": n11,
        "lr": None,
        "p": None,
    }
    if n00 + n01 == 0 or n10 + n11 == 0:
        return out  # no transition out of one state: the test is undefined, never "passed"
    p01, p11, pi = n01 / (n00 + n01), n11 / (n10 + n11), (n01 + n11) / (n00 + n01 + n10 + n11)
    l0 = _xlogy(n00 + n10, 1 - pi) + _xlogy(n01 + n11, pi)
    l1 = _xlogy(n00, 1 - p01) + _xlogy(n01, p01) + _xlogy(n10, 1 - p11) + _xlogy(n11, p11)
    lr = max(-2 * (l0 - l1), 0.0)
    out.update(lr=lr, p=chi2_sf(lr, 1))
    return out


# --------------------------------------------------------------------------- P&L distributions from quantile dots
def _knots(dots: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    d = np.asarray(dots, dtype=float)
    k = len(d)
    if k < 2 or not np.all(np.isfinite(d)) or np.any(np.diff(d) < 0):
        raise ValueError("quantile dots must be >= 2 finite, non-decreasing values")
    u = (np.arange(k) + 0.5) / k
    lo, hi = d[0] - (d[1] - d[0]) / 2, d[-1] + (d[-1] - d[-2]) / 2
    return np.concatenate(([0.0], u, [1.0])), np.concatenate(([lo], d, [hi]))


def quantile_fn(dots: Sequence[float]) -> Callable[[float], float]:
    u, q = _knots(dots)
    return lambda x: float(np.interp(x, u, q))


def crps(dots: Sequence[float], y: float, nodes: int = 2000) -> float:
    u, q = _knots(dots)
    m = (np.arange(nodes) + 0.5) / nodes
    qm = np.interp(m, u, q)
    return float(2 * np.mean(((y < qm).astype(float) - m) * (qm - y)))


def pit(dots: Sequence[float], y: float, clip: float = 1e-3) -> float:
    u, q = _knots(dots)
    if y < q[0]:
        return clip
    if y > q[-1]:
        return 1 - clip
    # F(y) = the largest u with q(u) <= y: interpolated between the knots around y; a flat stretch of q is a point
    # mass, and y on it gets the top of the stretch (the right-continuous CDF)
    j = int(np.searchsorted(q, y, side="right"))
    if j >= len(q):
        return 1 - clip
    i = j - 1
    v = float(u[i] + (u[j] - u[i]) * (y - q[i]) / (q[j] - q[i]))
    return min(max(v, clip), 1 - clip)


def var_es(dots: Sequence[float], alpha: float) -> tuple[float, float]:
    """(VaR, ES) at level alpha as SIGNED P&L values (module docstring); the trapezoid rule is exact here."""
    u, q = _knots(dots)
    va = float(np.interp(alpha, u, q))
    keep = u < alpha
    uu, qq = np.append(u[keep], alpha), np.append(q[keep], va)
    return va, float(np.sum((uu[1:] - uu[:-1]) * (qq[1:] + qq[:-1]) / 2) / alpha)

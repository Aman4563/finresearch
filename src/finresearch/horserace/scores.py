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
from statistics import NormalDist

import numpy as np

from finresearch.direction.metrics import _t_sf

N01 = NormalDist()


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


# --------------------------------------------------------------------------- tail tests on the PIT and the dots
def berkowitz(u: Sequence[float]) -> dict[str, float | int | None]:
    """Berkowitz (2001) LR (3 df) of z = Φ⁻¹(u) as AR(1) Gaussian z_t - μ = ρ(z_{t-1} - μ) + ε against i.i.d. N(0, 1),
    on the conditional likelihood of z_2..z_n (the conditional MLE is OLS of z_t on (1, z_{t-1}), σ² = SSR / m)."""
    z = np.array([N01.inv_cdf(x) for x in u], dtype=float)
    m = len(z) - 1
    out: dict[str, float | int | None] = {
        "n": len(z),
        "mu": None,
        "rho": None,
        "sigma": None,
        "lr": None,
        "p": None,
    }
    if m < 3 or float(np.var(z[:-1])) == 0:
        return out
    x, y = z[:-1], z[1:]
    rho = float(np.sum((x - x.mean()) * (y - y.mean())) / np.sum((x - x.mean()) ** 2))
    c = float(y.mean() - rho * x.mean())
    s2 = float(np.mean((y - c - rho * x) ** 2))
    if s2 <= 0:
        return out
    l1 = -m / 2 * math.log(2 * math.pi * s2) - m / 2
    l0 = -m / 2 * math.log(2 * math.pi) - float(np.sum(y**2)) / 2
    lr = max(2 * (l1 - l0), 0.0)
    mu = c / (1 - rho) if rho != 1 else None
    out.update(mu=mu, rho=rho, sigma=math.sqrt(s2), lr=lr, p=chi2_sf(lr, 3))
    return out


def _z2(x: np.ndarray, var: np.ndarray, es: np.ndarray, alpha: float) -> np.ndarray:
    return 1 - np.sum(x * (x < var) / es, axis=-1) / (x.shape[-1] * alpha)


def acerbi_szekely_z2(dots: Sequence[Sequence[float]], y: Sequence[float], alpha: float, sims: int, seed: int
                      ) -> dict[str, float | int | None]:  # fmt: skip
    """Acerbi & Szekely (2014) Z2 with ES_t the signed tail mean: Z2 = 1 - Σ X_t 1{X_t < VaR_t} / (T α ES_t), about 0
    under the model and negative when the tail risk was under-forecast. Events with ES_t >= 0 are excluded and
    counted. One-sided p = P(Z2_sim <= Z2) under each event's own quantile function (seeded)."""
    keep = [(d, v) for d, v in zip(dots, y, strict=True) if var_es(d, alpha)[1] < 0]
    out: dict[str, float | int | None] = {
        "n": len(keep),
        "excluded": len(dots) - len(keep),
        "z2": None,
        "p": None,
    }
    if not keep:
        return out
    ve = np.array([var_es(d, alpha) for d, _ in keep])
    x = np.array([v for _, v in keep], dtype=float)
    z2 = float(_z2(x, ve[:, 0], ve[:, 1], alpha))
    rng = np.random.default_rng(seed)
    uu = rng.random((sims, len(keep)))
    xs = np.column_stack([np.interp(uu[:, i], *_knots(d)) for i, (d, _) in enumerate(keep)])
    out.update(z2=z2, p=float(np.mean(_z2(xs, ve[:, 0], ve[:, 1], alpha) <= z2)))
    return out


# --------------------------------------------------------------------------- EV calibration and gate information
def t_quantile(q: float, df: int) -> float:
    """The q-quantile (q > 0.5) of Student's t with df degrees of freedom, by bisection on direction.metrics._t_sf."""
    lo, hi = 0.0, 1e6
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _t_sf(mid, df) > 1 - q else (lo, mid)
    return (lo + hi) / 2


def ols_clustered(x: Sequence[float], y: Sequence[float], clusters: Sequence[object], level: float = 0.95
                  ) -> dict[str, object]:  # fmt: skip
    """y = a + b x by OLS with the CR1 cluster-robust variance (X'X)⁻¹ Σ_g X_g'e_g e_g'X_g (X'X)⁻¹ · G/(G-1) ·
    (N-1)/(N-2) and t(G - 1) intervals. Everything None when N <= 2, G < 2 or x is constant (unknown, never 0)."""
    n, groups = len(x), sorted(set(clusters), key=str)
    out: dict[str, object] = {"n": n, "clusters": len(groups), "a": None, "b": None, "se_a": None, "se_b": None,
                              "t_crit": None, "ci_a": None, "ci_b": None, "t_b": None, "p_b": None}  # fmt: skip
    xa, ya = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if n <= 2 or len(groups) < 2 or float(np.var(xa)) == 0:
        return out
    big_x = np.column_stack([np.ones(n), xa])
    bread = np.linalg.inv(big_x.T @ big_x)
    a, b = bread @ big_x.T @ ya
    e = ya - big_x @ np.array([a, b])
    cl = np.array([groups.index(c) for c in clusters])
    meat = sum(np.outer(s, s) for s in (big_x[cl == g].T @ e[cl == g] for g in range(len(groups))))
    g = len(groups)
    v = bread @ meat @ bread * g / (g - 1) * (n - 1) / (n - 2)
    se_a, se_b = math.sqrt(max(v[0, 0], 0.0)), math.sqrt(max(v[1, 1], 0.0))
    tc = t_quantile(1 - (1 - level) / 2, g - 1)
    t_b = b / se_b if se_b > 0 else None
    out.update(a=float(a), b=float(b), se_a=se_a, se_b=se_b, t_crit=tc, ci_a=(a - tc * se_a, a + tc * se_a),
               ci_b=(b - tc * se_b, b + tc * se_b), t_b=t_b,
               p_b=None if t_b is None else min(1.0, 2 * _t_sf(abs(t_b), g - 1)))  # fmt: skip
    return out


def ev_calibration(
    ev: Sequence[float], pnl: Sequence[float], clusters: Sequence[object]
) -> dict[str, object]:
    """PnL = a + b EV: calibrated iff CI(b) contains 1 and CI(a) contains 0; None when the fit is unknown."""
    r = ols_clustered(ev, pnl, clusters)
    ci_a, ci_b = r["ci_a"], r["ci_b"]
    ok = None if ci_a is None or ci_b is None else bool(ci_b[0] <= 1 <= ci_b[1] and ci_a[0] <= 0 <= ci_a[1])  # type: ignore[index]
    return {**r, "calibrated": ok}


def gate_information(
    passed: Sequence[bool], pnl: Sequence[float], clusters: Sequence[object]
) -> dict[str, object]:
    """delta = mean P&L when the gate passes - when it fails (the dummy's OLS slope), expiry-clustered, two-sided."""
    r = ols_clustered([1.0 if p else 0.0 for p in passed], pnl, clusters)
    return {"delta": r["b"], "se": r["se_b"], "t": r["t_b"], "p": r["p_b"], "ci": r["ci_b"], "clusters": r["clusters"],
            "n_pass": sum(1 for p in passed if p), "n_fail": sum(1 for p in passed if not p)}  # fmt: skip


# --------------------------------------------------------------------------- exit reasons and drawdown bands
def multinomial_log_score(
    probs: Sequence[dict[str, float]], observed: Sequence[str], eps: float = 1e-6
) -> float:
    return sum(-math.log(max(p.get(o, 0.0), eps)) for p, o in zip(probs, observed, strict=True)) / len(
        observed
    )


def binom_two_sided(k: int, n: int, p: float) -> float:
    """Exact two-sided binomial p-value: the total probability of outcomes no more likely than k."""
    pm = [math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(n + 1)]
    return min(1.0, sum(x for x in pm if x <= pm[k] * (1 + 1e-9)))


def band_coverage(lo: Sequence[float], hi: Sequence[float], realised: Sequence[float], nominal: float
                  ) -> dict[str, float | int]:  # fmt: skip
    n = len(realised)
    k = sum(1 for a, b, r in zip(lo, hi, realised, strict=True) if a <= r <= b)
    return {"n": n, "inside": k, "share": k / n if n else float("nan"), "nominal": nominal,
            "p": binom_two_sided(k, n, nominal) if n else float("nan")}  # fmt: skip

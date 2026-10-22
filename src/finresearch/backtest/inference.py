"""Inference recalibration of the F&O backtest (#381, P2.6). Pure functions; each returns None (unknown) where its
statistic is undefined, never 0. Criteria v2 (backtest/criteria_v2.py) uses them as a SHADOW verdict beside the
registered one; nothing here changes a registered status.

- Variance of an estimated per-period Sharpe ratio: Var(SR̂) = (1 − γ₃·SR + (γ₄−1)/4 · SR²)/(T−1), the denominator of
  the deflated Sharpe ratio (Bailey & López de Prado 2014, eq. 2; metrics.sr_variance). Under the null of no skill
  (SR = 0) it is 1/(T−1) whatever the skew and kurtosis: V_null.
- Deflated Sharpe v2: SR₀ from eq. 1 with V = V_null, V_cand (the sample variance of the trials' out-of-sample daily
  SRs) or max(V_null, V_cand), and N = the number of trials whose SR enters V_cand (not VARIANTS_TRIED).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist
from typing import Any

import numpy as np

from finresearch.backtest.metrics import deflated_sharpe, expected_max_sharpe, sr_variance
from finresearch.skillmon.stats import block_len

N01 = NormalDist()

DSR_RULES = ("v_null", "v_cand", "max")
__all__ = ["DSR_RULES", "dsr_v2", "sr_variance"]


def _sample_var(xs: Sequence[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def dsr_v2(w: dict[str, Any], trial_srs: Sequence[float], rule: str = "max") -> dict[str, Any]:
    """DSR of one window `w` (daily_sr, days, skew, kurtosis as metrics.equity_stats gives them) against the trials'
    daily SRs under `rule` (module docstring). T = days − 1 daily returns."""
    if rule not in DSR_RULES:
        raise ValueError(f"unknown DSR rule {rule!r}; one of {DSR_RULES}")
    srs = [float(x) for x in trial_srs if x is not None]
    t = (w.get("days") or 0) - 1
    v_null = sr_variance(0.0, t, 0.0, 3.0)
    v_cand = _sample_var(srs)
    v = {"v_null": v_null, "v_cand": v_cand,
         "max": None if v_null is None or v_cand is None else max(v_null, v_cand)}[rule]  # fmt: skip
    out: dict[str, Any] = {"rule": rule, "n_trials": len(srs), "t": t, "v_null": v_null, "v_cand": v_cand,
                           "v_used": v, "sr0": None, "dsr": None}  # fmt: skip
    sr, sk, ku = w.get("daily_sr"), w.get("skew"), w.get("kurtosis")
    if v is None or sr is None or sk is None or ku is None or not srs:
        return out
    out["sr0"] = expected_max_sharpe(v, len(srs))
    out["dsr"] = deflated_sharpe(sr, t, sk, ku, len(srs), v)
    return out


def lo_eta(rhos: Sequence[float], q: int) -> float | None:
    """Lo (2002) eq. 20: η(q) = q / √(q + 2 Σ_{k=1}^{q−1} (q − k) ρ_k); ρ_k missing beyond len(rhos) count as 0.
    None when the radicand is not positive (the sample autocorrelations are inconsistent)."""
    rad = q + 2 * sum((q - k) * r for k, r in enumerate(rhos[: q - 1], start=1))
    return q / math.sqrt(rad) if rad > 0 else None


def nw_lag(n: int) -> int:
    """Newey & West (1994) plug-in lag ⌊4 (n/100)^(2/9)⌋, as portfolio.factor_exposures uses."""
    return int(4 * (n / 100) ** (2 / 9))


def lo_sharpe(returns: Sequence[float], q: int = 252, max_lag: int | None = None) -> dict[str, Any]:
    """Lo (2002) time-aggregated Sharpe SR(q) = η̂(q) · SR (eq. A18) with ρ̂_k = γ̂_k/γ̂_0, γ̂_k = (1/T) Σ (R_t − μ̂)
    (R_{t−k} − μ̂) (eq. A17), SR = μ̂/σ̂ (σ̂ with T − 1, as metrics.equity_stats). Lo's estimator uses all q − 1
    autocorrelations; with ~570 daily returns and q = 252 most are noise, so ρ̂_k beyond `max_lag` (default the
    Newey-West plug-in lag, nw_lag) are set to 0. This truncation is our choice, not the paper's."""
    n = len(returns)
    out: dict[str, Any] = {"q": q, "max_lag": None, "rho": None, "eta": None, "sharpe": None}
    if n < 3:
        return out
    mu = sum(returns) / n
    e = [x - mu for x in returns]
    g0 = sum(x * x for x in e) / n
    if g0 <= 0:
        return out
    lag = min(q - 1, n - 1, nw_lag(n) if max_lag is None else max_lag)
    rho = [sum(e[t] * e[t - k] for t in range(k, n)) / n / g0 for k in range(1, lag + 1)]
    eta = lo_eta(rho, q)
    sd = math.sqrt(sum(x * x for x in e) / (n - 1))
    out.update(max_lag=lag, rho=rho, eta=eta, sharpe=None if eta is None else mu / sd * eta)
    return out


def posterior_sharpe(sr: float | None, n_obs: int, skew: float | None, kurt: float | None, *, tau: float,
                     periods: int = 252) -> dict[str, Any]:  # fmt: skip
    """'Expected true' annual Sharpe under a skeptical Normal prior N(0, τ²) on the annual SR: E[SR | SR̂] =
    SR̂ · τ² / (τ² + se²), the Normal-Normal posterior mean (#381). se² = periods · (1 − γ₃ SR + (γ₄−1)/4 SR²)/T is the
    per-period sampling variance (Var(SR̂) as in DSR eq. 2, over T as the issue states it) scaled to a year."""
    out: dict[str, Any] = {
        "tau": tau,
        "sr_annual": None,
        "se2_annual": None,
        "shrink": None,
        "posterior": None,
    }
    if sr is None or skew is None or kurt is None or n_obs < 2:
        return out
    num = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if num <= 0:
        return out
    se2 = periods * num / n_obs
    k = tau * tau / (tau * tau + se2)
    sa = sr * math.sqrt(periods)
    out.update(sr_annual=sa, se2_annual=se2, shrink=k, posterior=sa * k)
    return out


# --------------------------------------------------------------------------- two-way clustering
def _cluster_var(e: Sequence[float], clusters: Sequence[Any]) -> tuple[float, int]:
    """One component of CGM eq. 2.15 for a mean (X = 1): c · Σ_g (Σ_{i∈g} e_i)² / n², c = G/(G−1) (their c1, c2, c3
    with K = 1). Returns (variance, G)."""
    sums: dict[Any, float] = {}
    for v, c in zip(e, clusters, strict=True):
        sums[c] = sums.get(c, 0.0) + v
    g, n = len(sums), len(e)
    if g < 2:
        return 0.0, g
    return g / (g - 1) * sum(s * s for s in sums.values()) / n**2, g


def twoway_clustered_t(values: Sequence[float], c1: Sequence[Any], c2: Sequence[Any]) -> dict[str, Any]:
    """t of the mean with the two-way cluster-robust variance of Cameron, Gelbach & Miller (2011) eq. 2.15:
    V = V_1 + V_2 − V_{1∩2}, each component with its own G/(G−1). When V is not positive, CGM's rule of thumb (p. 9):
    the larger one-way variance (`fallback` True). None when either dimension has fewer than 2 clusters."""
    n = len(values)
    out: dict[str, Any] = {"t": None, "var": None, "fallback": False, "g1": None, "g2": None}
    if n < 2:
        return out
    m = sum(values) / n
    e = [v - m for v in values]
    v1, g1 = _cluster_var(e, c1)
    v2, g2 = _cluster_var(e, c2)
    v12, _ = _cluster_var(e, list(zip(c1, c2, strict=True)))
    out.update(g1=g1, g2=g2)
    if g1 < 2 or g2 < 2:
        return out
    v = v1 + v2 - v12
    if v <= 0:
        v, out["fallback"] = max(v1, v2), True
    out.update(var=v, t=m / math.sqrt(v) if v > 0 else None)
    return out


# --------------------------------------------------------------------------- stationary bootstrap and BCa
def stationary_indices(n: int, mean_block: float, draws: int, rng: np.random.Generator) -> np.ndarray:
    """Politis & Romano (1994) stationary bootstrap: each resample starts at a uniform index; every next index is
    the following one (wrapping round, so the series is a circle) with probability 1 − 1/L, else a new uniform
    start: blocks of geometric length with mean L = `mean_block`. Shape (draws, n)."""
    p = 1.0 / mean_block
    new = rng.random((draws, n)) < p
    new[:, 0] = True
    starts = rng.integers(0, n, size=(draws, n))
    idx = np.empty((draws, n), dtype=np.int64)
    idx[:, 0] = starts[:, 0]
    for t in range(1, n):
        idx[:, t] = np.where(new[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return idx


def acceleration(jack: Sequence[float]) -> float | None:
    """Efron & Tibshirani (1993) eq. 14.15: a = Σ (θ̄ − θ_(i))³ / (6 (Σ (θ̄ − θ_(i))²)^(3/2)) over the jackknife
    (leave-one-out) values θ_(i), θ̄ their mean."""
    j = np.asarray(jack, dtype=float)
    d = j.mean() - j
    s2 = float(np.sum(d * d))
    return float(np.sum(d**3)) / (6 * s2**1.5) if s2 > 0 else None


def bca_levels(z0: float, a: float, level: float) -> tuple[float, float]:
    """Efron & Tibshirani (1993) eq. 14.10: α_1,2 = Φ(z0 + (z0 + z^(α)) / (1 − a (z0 + z^(α)))) at α = (1 − level)/2
    and 1 − α."""
    out = []
    for q in ((1 - level) / 2, 1 - (1 - level) / 2):
        z = N01.inv_cdf(q)
        out.append(N01.cdf(z0 + (z0 + z) / (1 - a * (z0 + z))))
    return out[0], out[1]


def bca_interval(theta: float, boot: np.ndarray, jack: Sequence[float], level: float = 0.95
                 ) -> tuple[float, float] | None:  # fmt: skip
    """The BCa interval (E&T eq. 14.9) from bootstrap replicates and jackknife values. z0 = Φ⁻¹(#{θ* < θ̂}/B)
    (eq. 14.14). None when z0 is infinite (every replicate on one side) or a is undefined."""
    b = np.asarray(boot, dtype=float)
    share = float(np.mean(b < theta))
    a = acceleration(jack)
    if share <= 0 or share >= 1 or a is None:
        return None
    lo, hi = bca_levels(N01.inv_cdf(share), a, level)
    return float(np.quantile(b, lo)), float(np.quantile(b, hi))


def cluster_bca(values: Sequence[float], clusters: Sequence[Any], draws: int, seed: int, level: float = 0.95,
                mean_block: float | None = None) -> dict[str, Any]:  # fmt: skip
    """BCa interval of the mean per trade: clusters (expiries) in time order (sorted by key, ISO dates sort in time)
    are resampled by the stationary bootstrap with mean block L = skillmon.stats.block_len(G, 1) = ⌈G^(1/3)⌉
    clusters unless given; the acceleration is the jackknife over clusters. A sample whose every replicate equals
    the mean is a point (lo = hi = mean)."""
    groups: dict[Any, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        groups.setdefault(c, []).append(v)
    keys = sorted(groups, key=str)
    g = len(keys)
    L = mean_block if mean_block is not None else (block_len(g, 1) if g else None)
    out: dict[str, Any] = {"lo": None, "hi": None, "clusters": g, "block": L, "draws": draws, "level": level}
    if g < 2:
        return out
    sums = np.array([sum(groups[k]) for k in keys])
    counts = np.array([len(groups[k]) for k in keys], dtype=float)
    theta = float(sums.sum() / counts.sum())
    idx = stationary_indices(g, float(L), draws, np.random.default_rng(seed))  # type: ignore[arg-type]
    boot = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    if np.allclose(boot, theta, rtol=0, atol=1e-12):
        out.update(lo=theta, hi=theta)
        return out
    jack = [(sums.sum() - sums[i]) / (counts.sum() - counts[i]) for i in range(g)]
    ci = bca_interval(theta, boot, jack, level)
    if ci is not None:
        out.update(lo=ci[0], hi=ci[1])
    return out

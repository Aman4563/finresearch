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
from typing import Any

from finresearch.backtest.metrics import deflated_sharpe, expected_max_sharpe, sr_variance

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
    out: dict[str, Any] = {"tau": tau, "sr_annual": None, "se2_annual": None, "shrink": None, "posterior": None}
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

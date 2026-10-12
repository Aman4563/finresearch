"""Backtest statistics (#307). Pure functions on the engine's trades and daily equity curve.

Trades are grouped by expiry ("clusters"): positions on the same expiry share one market outcome, so they are not
independent. The t-statistic of the expectancy uses the cluster-robust (sandwich) variance
Var(x̄) = G/(G−1) · Σ_g (Σ_{i∈g}(x_i − x̄))² / n², and the bootstrap resamples whole expiries (fixed seed).
Equity-curve statistics use daily returns r_t = E_t / E_{t−1} − 1 of the marked equity: Sharpe = mean/sd × √252,
Sortino = mean / downside deviation × √252 (downside deviation = √(mean(min(r,0)²))), CAGR = (E_end/E_start)^(365/days) − 1
on the capital (the margin pool), maximum drawdown = the largest fall from a running peak as % of the capital.
CVaR 5 % = the mean of the worst 5 % of trades (at least one trade).
Deflated Sharpe ratio: Bailey & López de Prado (2014) (specs.DEFLATED_SHARPE_SOURCE), eq. for the expected maximum
Sharpe of N trials SR₀ = √V · ((1−γ) Φ⁻¹(1−1/N) + γ Φ⁻¹(1−1/(N·e))) and
DSR = Φ((SR − SR₀) √(T−1) / √(1 − γ₃·SR + (γ₄−1)/4 · SR²)), SR per period (daily, not annualised), γ₃ skewness,
γ₄ kurtosis (3 for a normal), V the variance of the trials' Sharpe ratios, γ the Euler-Mascheroni constant.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from itertools import pairwise
from statistics import NormalDist
from typing import Any

import numpy as np

EULER_GAMMA = 0.5772156649015329
N01 = NormalDist()


def _r(x: float | None, d: int = 4) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), d)


def clustered_t(values: Sequence[float], clusters: Sequence[Any]) -> float | None:
    n = len(values)
    groups: dict[Any, list[float]] = defaultdict(list)
    for v, c in zip(values, clusters, strict=True):
        groups[c].append(v)
    g = len(groups)
    if n < 2 or g < 2:
        return None
    m = sum(values) / n
    var = g / (g - 1) * sum(sum(v - m for v in vs) ** 2 for vs in groups.values()) / n**2
    return m / math.sqrt(var) if var > 0 else None


def cluster_bootstrap(values: Sequence[float], clusters: Sequence[Any], draws: int, seed: int,
                      level: float = 0.95) -> tuple[float, float] | None:  # fmt: skip
    """Percentile CI of the mean per trade, resampling whole clusters with replacement."""
    groups: dict[Any, list[float]] = defaultdict(list)
    for v, c in zip(values, clusters, strict=True):
        groups[c].append(v)
    if len(groups) < 2:
        return None
    sums = np.array([sum(v) for v in groups.values()])
    counts = np.array([len(v) for v in groups.values()])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(sums), size=(draws, len(sums)))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def cvar(values: Sequence[float], q: float = 0.05) -> float | None:
    xs = sorted(values)
    if not xs:
        return None
    k = max(1, math.ceil(q * len(xs)))
    return sum(xs[:k]) / k


def trade_stats(trades: Sequence[dict[str, Any]], draws: int = 5000, seed: int = 307) -> dict[str, Any]:
    nets = [t["net"] for t in trades]
    clusters = [t["expiry"] for t in trades]
    n = len(nets)
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x < 0]
    sd = float(np.std(nets, ddof=1)) if n > 1 else None
    ci = cluster_bootstrap(nets, clusters, draws, seed)
    with_margin = [t for t in trades if t.get("margin")]
    rupee_years = sum(t["margin"] * max(t["days_held"], 1) / 365 for t in with_margin)
    return {
        "trades": n, "expiries": len(set(clusters)), "wins": len(wins), "losses": len(losses),
        "win_rate": _r(len(wins) / n) if n else None, "expectancy": _r(sum(nets) / n, 2) if n else None,
        "avg_win": _r(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": _r(sum(losses) / len(losses), 2) if losses else None,
        "worst_trade": _r(min(nets), 2) if nets else None, "best_trade": _r(max(nets), 2) if nets else None,
        "cvar_5": _r(cvar(nets), 2), "sd": _r(sd, 2), "total_net": _r(sum(nets), 2),
        "t_stat": _r(clustered_t(nets, clusters), 3),
        "ci_low": _r(ci[0], 2) if ci else None, "ci_high": _r(ci[1], 2) if ci else None,
        "costs": _r(sum(t["entry_costs"] + t["exit_costs"] + t["exercise_stt"] for t in trades), 2),
        "return_on_margin_annual": _r(sum(t["net"] for t in with_margin) / rupee_years) if rupee_years else None,
        "trades_without_margin": n - len(with_margin),
    }  # fmt: skip


def daily_returns(equity: Sequence[tuple[date, float]]) -> list[float]:
    return [b / a - 1 for (_, a), (_, b) in pairwise(equity) if a > 0]


def equity_stats(equity: Sequence[tuple[date, float]], capital: float) -> dict[str, Any]:
    if len(equity) < 2:
        return {"days": len(equity), "cagr": None, "max_drawdown_pct": None, "sharpe": None, "sortino": None,
                "daily_sr": None, "skew": None, "kurtosis": None}  # fmt: skip
    r = np.array(daily_returns(equity))
    span = (equity[-1][0] - equity[0][0]).days
    peak, dd = equity[0][1], 0.0
    for _, v in equity:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    sd = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    down = math.sqrt(float(np.mean(np.minimum(r, 0) ** 2)))
    mu = float(r.mean())
    skew = kurt = None
    if sd > 0:
        z = (r - mu) / float(r.std(ddof=0))
        skew, kurt = float(np.mean(z**3)), float(np.mean(z**4))
    growth = equity[-1][1] / equity[0][1]
    return {
        "days": len(equity), "cagr": _r(growth ** (365 / span) - 1) if span > 0 and growth > 0 else None,
        "max_drawdown_pct": _r(dd / capital * 100, 2), "max_drawdown": _r(dd, 2),
        "sharpe": _r(mu / sd * math.sqrt(252), 3) if sd > 0 else None,
        "sortino": _r(mu / down * math.sqrt(252), 3) if down > 0 else None,
        "daily_sr": _r(mu / sd, 6) if sd > 0 else None, "skew": _r(skew, 4), "kurtosis": _r(kurt, 4),
        "daily_cvar_5": _r(cvar(list(r)), 6),
    }  # fmt: skip


def expected_max_sharpe(var_trials: float, n_trials: int) -> float:
    if n_trials <= 1 or var_trials <= 0:
        return 0.0
    a = N01.inv_cdf(1 - 1 / n_trials)
    b = N01.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(var_trials) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe(
    sr: float, n_obs: int, skew: float, kurt: float, n_trials: int, var_trials: float
) -> float | None:
    """DSR (module docstring): the probability that the true Sharpe exceeds the best of N unskilled trials."""
    if n_obs < 2:
        return None
    sr0 = expected_max_sharpe(var_trials, n_trials)
    den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if den <= 0:
        return None
    return N01.cdf((sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(den))


def regime(pct: float | None) -> str:
    if pct is None:
        return "unknown"
    return "low" if pct < 1 / 3 else "mid" if pct < 2 / 3 else "high"


def breakdown(trades: Sequence[dict[str, Any]], key) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    return {k: trade_stats(v, draws=1000) for k, v in sorted(groups.items())}

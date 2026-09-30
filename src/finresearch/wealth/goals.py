"""Goal planning: a seeded Monte Carlo of the goal's savings with a monthly SIP that steps up once a year.

Model (stated, editable assumptions; not a forecast):
- Monthly steps m = 0..M-1. Each month the pot earns the month's return, then the SIP is added:
  V_{m+1} = V_m (1 + R_m) + SIP_m,   SIP_m = SIP_0 (1 + g)^floor(m / 12).
- R_m = sum_c w_c(m) (exp(x_c) - 1), with x_c ~ N(ln(1 + mu_c)/12 - s_c^2/2, s_c^2), s_c = sigma_c / sqrt(12): a
  lognormal whose expected yearly growth is 1 + mu_c. Classes are drawn independently (a limitation: equity and gold
  are not independent in reality); the pot is rebalanced to the weights every month.
- Weights: the goal's own equity % if set, else a time-to-goal glide (rule of thumb [W]): more than 7 years left
  70 % equity, 3-7 years 50 %, 1-3 years 20 %, under a year 0 %; the goal's gold % is held throughout; the rest
  debt.
- Target in rupees of the goal date: T = target_today (1 + inflation)^(M / 12).
- P(success) = share of paths with V_M >= T, with a 95 % Wilson interval for the simulation's own sampling error,
  and the same run with equity returns cut by 2 pp (the "haircut" row) as the stated range of assumption risk.
- SIP needed for 75 % / 90 % success: bisection on SIP_0 over the same random draws (common random numbers), so the
  answer moves only with the SIP.

Reproducible: numpy.random.Generator(PCG64(seed)); the same inputs and seed give identical numbers.
Evidence: Monte Carlo success probability is the standard planning output (Vanguard, Morningstar tools) [U]; the
lognormal misses fat tails and sequence effects beyond its volatility [K]; above ~90 % success usually means
over-saving [W].
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

HAIRCUT_PP = 2.0
Z95 = 1.959963984540054


@dataclass(frozen=True)
class ClassAssumption:
    mu_pct: float  # expected yearly return, %
    sigma_pct: float  # yearly volatility, %
    note: str = ""


# Defaults: round long-run assumptions for Indian assets, labelled as assumptions [K]; editable on /wealth.
DEFAULT_ASSUMPTIONS: dict[str, ClassAssumption] = {
    "equity": ClassAssumption(
        11.0,
        17.0,
        "Broad Indian equity, long-run assumption [K]; Nifty 50 volatility has "
        "been in the mid-to-high teens [U]",
    ),
    "debt": ClassAssumption(7.0, 2.5, "Short-duration debt / FD-like, assumption [K]"),
    "gold": ClassAssumption(8.0, 15.0, "Gold in rupees, assumption [K]"),
}


def assumptions_from(raw: dict[str, Any] | None) -> dict[str, ClassAssumption]:
    out = dict(DEFAULT_ASSUMPTIONS)
    for k, v in (raw or {}).items():
        if k in out and isinstance(v, dict):
            mu, sd = float(v.get("mu_pct", out[k].mu_pct)), float(v.get("sigma_pct", out[k].sigma_pct))
            if not (-20 <= mu <= 40 and 0 <= sd <= 80):
                raise ValueError(f"{k}: return must be within -20..40 % and volatility within 0..80 %")
            out[k] = ClassAssumption(mu, sd, "your assumption")
    return out


def glide_equity(years_left: float) -> float:
    """Time-to-goal equity share, % (rule of thumb [W])."""
    if years_left > 7:
        return 70.0
    if years_left >= 3:
        return 50.0
    if years_left >= 1:
        return 20.0
    return 0.0


def weights_path(months: int, equity_pct: float | None, gold_pct: float) -> np.ndarray:
    """(months, 3) weights for equity, debt, gold."""
    w = np.zeros((months, 3))
    for m in range(months):
        e = equity_pct if equity_pct is not None else glide_equity((months - m) / 12)
        g = min(gold_pct, 100 - e)
        w[m] = (e / 100, (100 - e - g) / 100, g / 100)
    return w


def sip_path(sip0: float, step_up_pct: float, months: int) -> np.ndarray:
    return np.array([sip0 * (1 + step_up_pct / 100) ** (m // 12) for m in range(months)])


def monthly_returns(
    months: int,
    n: int,
    a: dict[str, ClassAssumption],
    weights: np.ndarray,
    seed: int,
    equity_haircut_pp: float = 0.0,
) -> np.ndarray:
    """(n, months) portfolio returns. The standard-normal draws depend only on (seed, n, months), so a haircut run
    or a different SIP reuses the same randomness."""
    rng = np.random.Generator(np.random.PCG64(seed))
    z = rng.standard_normal((3, n, months))
    out = np.zeros((n, months))
    for j, key in enumerate(("equity", "debt", "gold")):
        mu = a[key].mu_pct / 100 - (equity_haircut_pp / 100 if key == "equity" else 0.0)
        s = a[key].sigma_pct / 100 / math.sqrt(12)
        drift = math.log1p(mu) / 12 - s * s / 2
        out += weights[:, j] * np.expm1(drift + s * z[j])
    return out


def terminal(start: float, sips: np.ndarray, returns: np.ndarray) -> np.ndarray:
    v = np.full(returns.shape[0], float(start))
    for m in range(returns.shape[1]):
        v = v * (1 + returns[:, m]) + sips[m]
    return v


def paths_by_year(start: float, sips: np.ndarray, returns: np.ndarray) -> list[np.ndarray]:
    """Values at every 12th month and at the end (for the fan chart)."""
    v = np.full(returns.shape[0], float(start))
    out = [v.copy()]
    months = returns.shape[1]
    for m in range(months):
        v = v * (1 + returns[:, m]) + sips[m]
        if (m + 1) % 12 == 0 or m + 1 == months:
            out.append(v.copy())
    return out


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def fv_closed_form(
    start: float, sip0: float, step_up_pct: float, months: int, annual_return_pct: float
) -> float:
    """Deterministic value (zero volatility, one constant return): V0 (1+r)^M + sum_m SIP_m (1+r)^(M-m-1)."""
    r = (1 + annual_return_pct / 100) ** (1 / 12) - 1
    total = start * (1 + r) ** months
    for m in range(months):
        total += sip0 * (1 + step_up_pct / 100) ** (m // 12) * (1 + r) ** (months - m - 1)
    return total


def required_sip(
    start: float, step_up_pct: float, target: float, returns: np.ndarray, p: float, hi: float | None = None
) -> float | None:
    """Smallest SIP_0 (to the rupee) whose success share over these draws is >= p. None if even a very large SIP
    misses (e.g. a goal due within a month)."""
    months = returns.shape[1]
    if months == 0:
        return None

    def ok(s: float) -> bool:
        return float(np.mean(terminal(start, sip_path(s, step_up_pct, months), returns) >= target)) >= p

    if ok(0.0):
        return 0.0
    hi = hi or max(target / months, 1000.0)
    for _ in range(40):
        if ok(hi):
            break
        hi *= 2
    else:
        return None
    lo = 0.0
    while hi - lo > 1:
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
    return math.ceil(hi)


@dataclass
class GoalPlan:
    months: int
    target_today: float
    target_nominal: float
    start: float
    sip0: float
    step_up_pct: float
    n: int
    seed: int
    p_success: float
    p_ci: tuple[float, float]
    p_haircut: float
    p_plus_year: float | None
    p_step_up_plus5: float | None
    sip_for_75: float | None
    sip_for_90: float | None
    fan: list[dict[str, float]] = field(default_factory=list)
    terminal_pcts: dict[str, float] = field(default_factory=dict)
    assumptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    equity_path: str = ""

    def to_json(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["p_ci"] = list(self.p_ci)
        return d


def plan(
    *,
    start: float,
    sip0: float,
    step_up_pct: float,
    months: int,
    target_today: float,
    inflation_pct: float,
    equity_pct: float | None,
    gold_pct: float,
    assumptions: dict[str, ClassAssumption],
    n: int = 5000,
    seed: int = 20260930,
    extras: bool = True,
    today_year: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> GoalPlan:
    if months <= 0:
        raise ValueError("the goal date must be at least a month away")
    target = target_today * (1 + inflation_pct / 100) ** (months / 12)
    w = weights_path(months, equity_pct, gold_pct)
    sips = sip_path(sip0, step_up_pct, months)
    rets = monthly_returns(months, n, assumptions, w, seed)
    years = paths_by_year(start, sips, rets)
    final = years[-1]
    k = int(np.sum(final >= target))
    p = k / n
    hair = monthly_returns(months, n, assumptions, w, seed, equity_haircut_pp=HAIRCUT_PP)
    p_hair = float(np.mean(terminal(start, sips, hair) >= target))
    p_year = p_step = None
    if extras:
        # one more year: the same goal (in today's rupees, inflated one more year) and one more year of SIPs
        m2 = months + 12
        t2 = target_today * (1 + inflation_pct / 100) ** (m2 / 12)
        r2 = monthly_returns(m2, n, assumptions, weights_path(m2, equity_pct, gold_pct), seed)
        p_year = float(np.mean(terminal(start, sip_path(sip0, step_up_pct, m2), r2) >= t2))
        p_step = float(np.mean(terminal(start, sip_path(sip0, step_up_pct + 5, months), rets) >= target))
    fan = []
    for i, v in enumerate(years):
        m = min(i * 12, months)
        fan.append(
            {
                "month": m,
                "p10": float(np.percentile(v, 10)),
                "p50": float(np.percentile(v, 50)),
                "p90": float(np.percentile(v, 90)),
                "target": target_today * (1 + inflation_pct / 100) ** (m / 12),
            }
        )
    eq = (
        f"{equity_pct:g} % equity throughout"
        if equity_pct is not None
        else "time-to-goal glide: >7 y 70 %, 3-7 y 50 %, 1-3 y 20 %, <1 y 0 % equity (rule of thumb)"
    )
    return GoalPlan(
        months=months,
        target_today=target_today,
        target_nominal=target,
        start=start,
        sip0=sip0,
        step_up_pct=step_up_pct,
        n=n,
        seed=seed,
        p_success=p,
        p_ci=wilson(k, n),
        p_haircut=p_hair,
        p_plus_year=p_year,
        p_step_up_plus5=p_step,
        sip_for_75=required_sip(start, step_up_pct, target, rets, 0.75) if extras else None,
        sip_for_90=required_sip(start, step_up_pct, target, rets, 0.90) if extras else None,
        fan=fan,
        terminal_pcts={q: float(np.percentile(final, int(q[1:]))) for q in ("p10", "p50", "p90")},
        assumptions={
            k2: {"mu_pct": a.mu_pct, "sigma_pct": a.sigma_pct, "note": a.note}
            for k2, a in assumptions.items()
        },
        equity_path=f"{eq}; gold {gold_pct:g} %; rest debt",
    )

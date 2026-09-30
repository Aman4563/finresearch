"""Options arithmetic for analysis (never order placement): Black–Scholes prices and greeks, implied volatility,
multi-leg strategy payoffs at expiry, breakevens, maximum profit and loss, and probability of profit.

Conventions: continuous compounding; `rate` and `div_yield` are annual fractions; `vol` is annual; `t_years` is the
time to expiry in years (calendar days / 365). Vega is per 1 volatility point (0.01); theta is per calendar day;
rho is per 1 rate point. European exercise is assumed (Indian index and stock options are European).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Right = Literal["call", "put"]


def _n(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1d2(s: float, k: float, t: float, r: float, q: float, v: float) -> tuple[float, float]:
    if s <= 0 or k <= 0 or t <= 0 or v <= 0:
        raise ValueError("spot, strike, time and volatility must be positive")
    d1 = (math.log(s / k) + (r - q + v * v / 2) * t) / (v * math.sqrt(t))
    return d1, d1 - v * math.sqrt(t)


def bs_price(right: Right, spot: float, strike: float, t_years: float, rate: float, vol: float,
             div_yield: float = 0.0) -> float:  # fmt: skip
    d1, d2 = _d1d2(spot, strike, t_years, rate, div_yield, vol)
    df, dq = math.exp(-rate * t_years), math.exp(-div_yield * t_years)
    if right == "call":
        return spot * dq * _n(d1) - strike * df * _n(d2)
    return strike * df * _n(-d2) - spot * dq * _n(-d1)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float  # per 1 vol point
    theta: float  # per calendar day
    rho: float  # per 1 rate point


def greeks(right: Right, spot: float, strike: float, t_years: float, rate: float, vol: float,
           div_yield: float = 0.0) -> Greeks:  # fmt: skip
    s, k, t, r, q, v = spot, strike, t_years, rate, div_yield, vol
    d1, d2 = _d1d2(s, k, t, r, q, v)
    df, dq = math.exp(-r * t), math.exp(-q * t)
    gamma = dq * _pdf(d1) / (s * v * math.sqrt(t))
    vega = s * dq * _pdf(d1) * math.sqrt(t) / 100
    common = -s * dq * _pdf(d1) * v / (2 * math.sqrt(t))
    if right == "call":
        delta = dq * _n(d1)
        theta = common - r * k * df * _n(d2) + q * s * dq * _n(d1)
        rho = k * t * df * _n(d2) / 100
    else:
        delta = -dq * _n(-d1)
        theta = common + r * k * df * _n(-d2) - q * s * dq * _n(-d1)
        rho = -k * t * df * _n(-d2) / 100
    return Greeks(bs_price(right, s, k, t, r, v, q), delta, gamma, vega, theta / 365, rho)


def implied_vol(right: Right, price: float, spot: float, strike: float, t_years: float, rate: float,
                div_yield: float = 0.0) -> float | None:  # fmt: skip
    """Volatility that reproduces `price` (bisection on 0.1%–500%); None when the price is outside no-arbitrage
    bounds."""
    lo, hi = 0.001, 5.0
    f = lambda v: bs_price(right, spot, strike, t_years, rate, v, div_yield) - price  # noqa: E731
    if f(lo) > 0 or f(hi) < 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


@dataclass(frozen=True)
class Leg:
    """One position: `right` call/put/future, long (+qty) or short (-qty) in units (lots x lot size)."""

    right: Literal["call", "put", "future"]
    strike: float  # for a future: the entry price
    premium: float  # option premium paid (+) / received for shorts is handled by the quantity sign
    qty: int


def leg_payoff(leg: Leg, price_at_expiry: float) -> float:
    if leg.right == "future":
        return (price_at_expiry - leg.strike) * leg.qty
    intrinsic = (
        max(0.0, price_at_expiry - leg.strike)
        if leg.right == "call"
        else max(0.0, leg.strike - price_at_expiry)
    )
    return (intrinsic - leg.premium) * leg.qty


def payoff(legs: Sequence[Leg], price_at_expiry: float) -> float:
    return sum(leg_payoff(x, price_at_expiry) for x in legs)


@dataclass(frozen=True)
class StrategyProfile:
    breakevens: list[float]
    max_profit: float | None  # None = unlimited
    max_loss: float | None  # None = unlimited (a negative number otherwise)
    net_premium: float  # paid (+) or received (-)
    curve: list[tuple[float, float]]


def profile(legs: Sequence[Leg], spot: float, width: float = 0.3, points: int = 241) -> StrategyProfile:
    """Payoff at expiry over spot ± `width` (fraction), breakevens and bounded/unbounded extremes."""
    if not legs:
        raise ValueError("a strategy needs at least one leg")
    strikes = sorted({x.strike for x in legs})
    lo, hi = min(spot * (1 - width), strikes[0] * 0.9), max(spot * (1 + width), strikes[-1] * 1.1)
    grid = sorted({*(lo + (hi - lo) * i / (points - 1) for i in range(points)), *strikes})
    curve = [(p, payoff(legs, p)) for p in grid]
    breakevens = []
    for (p0, v0), (p1, v1) in itertools.pairwise(curve):
        if v0 == 0:
            breakevens.append(round(p0, 2))
        elif v0 * v1 < 0:
            breakevens.append(round(p0 + (p1 - p0) * (-v0) / (v1 - v0), 2))
    # prices cannot fall below zero, so only the upside can be unbounded: its slope is the net call + future quantity
    up_slope = sum(x.qty for x in legs if x.right in ("call", "future"))
    values = [v for _, v in curve] + [payoff(legs, 0.0)]
    max_profit = None if up_slope > 0 else max(values)
    max_loss = None if up_slope < 0 else min(values)
    net = sum(x.premium * x.qty for x in legs if x.right != "future")
    return StrategyProfile(sorted(set(breakevens)), max_profit, max_loss, net, curve)


def probability_of_profit(legs: Sequence[Leg], spot: float, t_years: float, vol: float, rate: float = 0.0,
                          steps: int = 2000, drift: float | None = None) -> float:  # fmt: skip
    """Probability that the payoff at expiry is positive under a lognormal price with `vol`. Risk-neutral (drift =
    `rate`) by default; pass `drift` (annual μ) for a real-world probability. Exact: the payoff is piecewise linear
    between strikes, so the profit region is a union of intervals whose probabilities are N(d2*) differences.
    (`steps` is kept for callers of the earlier grid version and is unused.)"""
    del steps
    return prob_positive(lambda p: payoff(legs, p), [x.strike for x in legs], spot, t_years, vol,
                         rate if drift is None else drift)  # fmt: skip


def profit_intervals(fn: Callable[[float], float], kinks: Sequence[float]) -> list[tuple[float, float]]:
    """Where a piecewise-linear `fn` of the price (kinks only at `kinks`) is > 0, as [(low, high)] with high = inf
    for an open-ended interval."""
    pts = sorted({0.0, *(float(k) for k in kinks if k > 0)})
    out: list[tuple[float, float]] = []
    for a, b in [*itertools.pairwise(pts), (pts[-1], math.inf)]:
        end = a + 1.0 if b == math.inf else b  # beyond the last kink: the line through a and a + 1
        fa, fe = fn(a), fn(end)
        pos_b = fe > fa if b == math.inf and fe != fa else (fe > 0 if b != math.inf else fa > 0)
        if fa > 0 and pos_b:
            lo, hi = a, b
        elif fa > 0 or pos_b:
            root = a + (end - a) * fa / (fa - fe)
            lo, hi = (a, root) if fa > 0 else (root, b)
        else:
            continue
        if out and abs(out[-1][1] - lo) < 1e-9:
            out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def prob_positive(fn: Callable[[float], float], kinks: Sequence[float], spot: float, t_years: float, vol: float,
                  drift: float) -> float:  # fmt: skip
    """P(fn(S_T) > 0) for a piecewise-linear fn under the lognormal price: Σ [N(d2*(low)) − N(d2*(high))]."""

    def above(k: float) -> float:
        return 1.0 if k <= 0 else 0.0 if k == math.inf else prob_above(spot, k, t_years, vol, drift)

    return sum(above(lo) - above(hi) for lo, hi in profit_intervals(fn, kinks))


# --------------------------------------------------------------------------- model probabilities, EV and risk
# docs/dev/RESEARCH_ROADMAP.md §D.5. All results are model probabilities under a lognormal price, not forecasts.


def prob_above(
    spot: float, strike: float, t_years: float, vol: float, drift: float, div_yield: float = 0.0
) -> float:
    """P(S_T > K) = N(d2*), d2* = [ln(S/K) + (μ − q − σ²/2)T] / (σ√T). With μ = r this is the risk-neutral N(d2)."""
    _, d2 = _d1d2(spot, strike, t_years, drift, div_yield, vol)
    return _n(d2)


def lognormal_grid(spot: float, t_years: float, vol: float, drift: float, steps: int = 2000,
                   div_yield: float = 0.0) -> list[tuple[float, float]]:  # fmt: skip
    """(price at expiry, probability mass) on a ±6σ midpoint grid of ln S_T ~ N(ln S + (μ − q − σ²/2)T, σ²T)."""
    if spot <= 0 or t_years <= 0 or vol <= 0:
        raise ValueError("spot, time and volatility must be positive")
    mu = (drift - div_yield - vol * vol / 2) * t_years
    sd = vol * math.sqrt(t_years)
    out = []
    for i in range(steps):
        z = -6 + 12 * (i + 0.5) / steps
        out.append((spot * math.exp(mu + sd * z), _pdf(z) * 12 / steps))
    return out


def expected_move(spot: float, vol: float, t_years: float) -> tuple[float, float, float]:
    """The 1-σ expected move to expiry, ±S·σ·√T, and its band (roadmap §D.5): (move, low, high)."""
    m = spot * vol * math.sqrt(t_years)
    return m, spot - m, spot + m


def realised_vol(closes: Sequence[float], window: int | None = None, periods_per_year: int = 252) -> float:
    """Annualised close-to-close volatility: sample standard deviation of the last `window` daily log returns
    × √periods_per_year."""
    c = [float(x) for x in closes if x]
    r = [math.log(b / a) for a, b in itertools.pairwise(c)]
    if window:
        r = r[-window:]
    if len(r) < 2:
        raise ValueError("need at least three closes for a volatility")
    mean = sum(r) / len(r)
    return math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1) * periods_per_year)


def _gross(legs: Sequence[Leg], price: float) -> float:
    """Value received at expiry, before premiums: intrinsic values (and futures P&L) × quantities."""
    return payoff(legs, price) + sum(x.premium * x.qty for x in legs if x.right != "future")


@dataclass(frozen=True)
class Outcome:
    """The P&L distribution at expiry under one volatility and drift, after costs."""

    vol: float
    drift: float
    pop: float  # P(P&L after costs > 0), exact (piecewise-linear P&L; `exit_cost` must be linear between strikes)
    ev: float  # present value of the expected P&L after costs (discounted at the risk-free rate)
    ev_gross: float  # the same before costs
    exit_cost: float  # expected cost at expiry (STT on exercised long options), in the EV
    quantiles: dict[str, float]  # P&L after costs at expiry


def outcome(legs: Sequence[Leg], spot: float, t_years: float, vol: float, drift: float, rate: float,
            entry_cost: float = 0.0, exit_cost: Callable[[float], float] | None = None,
            qs: Sequence[float] = (0.1, 0.25, 0.5, 0.75, 0.9), steps: int = 4000) -> Outcome:  # fmt: skip
    """P(profit), expected value and P&L quantiles of holding `legs` to expiry.

    EV = e^(−rT)·E[gross payoff at expiry] − net premium − costs. Under the risk-neutral measure (drift = rate) with
    premiums at Black–Scholes fair value, E_Q[gross] = e^(rT)·premium, so EV = −costs: *at fair prices the expected
    P&L of any strategy is minus its costs* (roadmap §D.5). The quantiles are of the undiscounted P&L after costs; the
    P&L is not monotonic in the price, so they come from the sorted P&L masses, not from price quantiles."""
    net = sum(x.premium * x.qty for x in legs if x.right != "future")
    grid = lognormal_grid(spot, t_years, vol, drift, steps)
    df = math.exp(-rate * t_years)
    e_gross = e_exit = 0.0
    dist = []
    for price, w in grid:
        g = _gross(legs, price)
        xc = exit_cost(price) if exit_cost else 0.0
        e_gross += g * w
        e_exit += xc * w
        pnl = g - net - entry_cost - xc
        dist.append((pnl, w))
    total = sum(w for _, w in grid)  # 1 − (mass beyond ±6σ)
    dist.sort()
    quant, acc, i = {}, 0.0, 0
    for q in sorted(qs):
        while i < len(dist) - 1 and acc + dist[i][1] < q * total:
            acc += dist[i][1]
            i += 1
        quant[f"p{round(q * 100)}"] = dist[i][0]
    ev_gross = df * e_gross / total - net
    exact = prob_positive(lambda p: _gross(legs, p) - net - entry_cost - (exit_cost(p) if exit_cost else 0.0),
                          [x.strike for x in legs], spot, t_years, vol, drift)  # fmt: skip
    return Outcome(vol, drift, exact, ev_gross - entry_cost - df * e_exit / total, ev_gross,
                   e_exit / total, quant)  # fmt: skip


def pnl_distribution(legs: Sequence[Leg], spot: float, t_years: float, vol: float, drift: float,
                     entry_cost: float = 0.0, exit_cost: Callable[[float], float] | None = None,
                     steps: int = 400) -> list[tuple[float, float]]:  # fmt: skip
    """(P&L after costs, probability) pairs at expiry on a coarse grid, for simulations."""
    net = sum(x.premium * x.qty for x in legs if x.right != "future")
    grid = lognormal_grid(spot, t_years, vol, drift, steps)
    total = sum(w for _, w in grid)
    return [
        (_gross(legs, p) - net - entry_cost - (exit_cost(p) if exit_cost else 0.0), w / total)
        for p, w in grid
    ]


def risk_of_ruin(edge: float, units: float) -> float:
    """Classic gambler's-ruin approximation for even-money bets with edge e and capital of C/u bet units:
    RoR ≈ ((1 − e)/(1 + e))^(C/u); 1 when there is no positive edge (roadmap §D.5)."""
    if edge <= 0:
        return 1.0
    if edge >= 1:
        return 0.0
    return ((1 - edge) / (1 + edge)) ** units


def drawdown_probability(dist: Sequence[tuple[float, float]], capital: float, threshold: float, repeats: int,
                         sims: int = 4000, seed: int = 7) -> float:  # fmt: skip
    """Monte Carlo: P(the running P&L falls more than `threshold` × capital below its peak (or the start) within
    `repeats` independent repetitions of the strategy), each drawn from `dist`. Seeded, so reproducible."""
    import random

    if capital <= 0 or repeats < 1 or not dist:
        raise ValueError("capital, repeats and a distribution are required")
    rng = random.Random(seed)
    values, weights = [x for x, _ in dist], [w for _, w in dist]
    limit, hit = threshold * capital, 0
    for _ in range(sims):
        eq = peak = 0.0
        for x in rng.choices(values, weights, k=repeats):
            eq += x
            peak = max(peak, eq)
            if peak - eq > limit:
                hit += 1
                break
    return hit / sims


def _inr(x: float) -> str:
    """Whole rupees with Indian digit grouping (12,34,567)."""
    from finresearch.fincalc.numbers import group_indian

    return ("-" if x < 0 else "") + group_indian(str(round(abs(x))))


@dataclass(frozen=True)
class CapitalCheck:
    max_loss: float | None  # rupees, positive; None = unlimited
    capital: float
    pct_of_capital: float | None
    limit_pct: float
    within: bool
    message: str


def max_loss_check(max_loss: float | None, capital: float, limit_pct: float) -> CapitalCheck:
    """Max loss as a % of the user's F&O capital against the profile's limit (roadmap: e.g. ≤ 2 %). An unlimited
    loss never passes."""
    if max_loss is None:
        return CapitalCheck(None, capital, None, limit_pct, False,
                            "Unlimited loss (a naked short option): the loss can exceed any capital.")  # fmt: skip
    loss = abs(min(0.0, max_loss))
    if capital <= 0:
        return CapitalCheck(loss, capital, None, limit_pct, False, "No F&O capital set in the profile.")
    pct = loss / capital * 100
    ok = pct <= limit_pct
    msg = (f"Max loss ₹{_inr(loss)} is {pct:.1f}% of your ₹{_inr(capital)} F&O capital "
           f"({'within' if ok else 'above'} your {limit_pct:g}% limit).")  # fmt: skip
    return CapitalCheck(loss, capital, pct, limit_pct, ok, msg)


def to_decimal(x: float, places: int = 4) -> Decimal:
    return Decimal(str(round(x, places)))

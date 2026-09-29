"""Options arithmetic for analysis (never order placement): Black–Scholes prices and greeks, implied volatility,
multi-leg strategy payoffs at expiry, breakevens, maximum profit and loss, and probability of profit.

Conventions: continuous compounding; `rate` and `div_yield` are annual fractions; `vol` is annual; `t_years` is the
time to expiry in years (calendar days / 365). Vega is per 1 volatility point (0.01); theta is per calendar day;
rho is per 1 rate point. European exercise is assumed (Indian index and stock options are European).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
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
                          steps: int = 2000) -> float:  # fmt: skip
    """Risk-neutral probability that the payoff at expiry is positive, under a lognormal price with `vol`."""
    mu = (rate - vol * vol / 2) * t_years
    sd = vol * math.sqrt(t_years)
    total = 0.0
    for i in range(steps):
        z = -6 + 12 * (i + 0.5) / steps
        price = spot * math.exp(mu + sd * z)
        if payoff(legs, price) > 0:
            total += _pdf(z) * 12 / steps
    return total


def to_decimal(x: float, places: int = 4) -> Decimal:
    return Decimal(str(round(x, places)))

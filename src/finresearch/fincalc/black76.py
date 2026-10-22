"""The app's own Black-76 engine (#385, P3.2): one pricing convention for European options on a forward, parameterised
by the forward F and the discount factor DF to the option's payment date, never by an assumed rate.

    C = DF [F N(d1) - K N(d2)],  P = DF [K N(-d2) - F N(-d1)],  d1 = [ln(F/K) + sigma^2 T/2] / (sigma sqrt T)

T is whatever clock the caller's sigma is quoted on (calendar years, or business years from fincalc.bustime): only
the total variance sigma^2 T enters a price. With DF = e^(-rT) and F = S e^((r - q)T) it is Black-Scholes with the
dividend yield q, so the spot delta is e^(-qT) N(d1) = (F DF / S) N(d1) with no q assumed.

Pure functions; nothing here reads a rate. A caller without a dated rate or an identified parity discount factor has
no DF, and every function that needs one then answers unknown with the reason (`reason`), never a default rate.
The live card and the registered backtest still price with fincalc.options / fno_history; switching them is the
horse race's decision (#377).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from finresearch.fincalc.options import Right

# The implied-volatility solver: a Newton iteration kept inside a bracket that always holds the root (a step that
# leaves the bracket, or a vega too small to divide by, is replaced by bisection). Stated tolerances:
SIGMA_LO, SIGMA_HI = 1e-4, 10.0  # the bracket: 0.01 % to 1,000 % a year; a price outside it is unknown
PRICE_TOL = 1e-10  # converged when |model - target| <= PRICE_TOL x the out-of-the-money twin's price (relative)
SIGMA_TOL = 1e-10  # ... or when the bracket is narrower than this (annual volatility, as a fraction)
MAX_ITER = 100  # Newton halves the bracket at worst, so 100 steps cannot run out on a valid bracket
MAX_DF = 1.05  # a discount factor above this (a rate below about -5 % over a year) is refused as an input error


def _cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))  # erfc keeps the lower tail accurate (1 - erf cancels)


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _intrinsic(right: Right, fwd: float, strike: float) -> float:
    return max(0.0, fwd - strike) if right == "call" else max(0.0, strike - fwd)


def _d1(fwd: float, strike: float, t: float, sigma: float) -> float:
    sd = sigma * math.sqrt(t)
    return (math.log(fwd / strike) + sd * sd / 2) / sd


def price(right: Right, fwd: float, strike: float, t: float, df: float, sigma: float) -> float:
    """The Black-76 value; sigma = 0 or T = 0 gives the discounted intrinsic value."""
    if sigma <= 0 or t <= 0:
        return df * _intrinsic(right, fwd, strike)
    d1 = _d1(fwd, strike, t, sigma)
    d2 = d1 - sigma * math.sqrt(t)
    if right == "call":
        return df * (fwd * _cdf(d1) - strike * _cdf(d2))
    return df * (strike * _cdf(-d2) - fwd * _cdf(-d1))


def vega(fwd: float, strike: float, t: float, df: float, sigma: float) -> float:
    """dPrice/dsigma per unit of sigma (1.00 = 100 vol points): DF F n(d1) sqrt T, the same for a call and a put."""
    if sigma <= 0 or t <= 0:
        return 0.0
    return df * fwd * _pdf(_d1(fwd, strike, t, sigma)) * math.sqrt(t)


def forward_delta(right: Right, fwd: float, strike: float, t: float, df: float, sigma: float) -> float:
    """dPrice/dF: DF N(d1) for a call, -DF N(-d1) for a put."""
    d1 = _d1(fwd, strike, t, sigma)
    return df * _cdf(d1) if right == "call" else -df * _cdf(-d1)


def spot_delta(right: Right, fwd: float, strike: float, t: float, df: float, sigma: float, spot: float) -> float:
    """dPrice/dS with the carry the forward implies: e^(-qT) N(d1) = (F DF / S) N(d1) for a call."""
    return forward_delta(right, fwd, strike, t, df, sigma) * fwd / spot


def bounds(right: Right, fwd: float, strike: float, df: float) -> tuple[float, float]:
    """No-arbitrage bounds of a European option on a forward: DF x intrinsic < price < DF x F (call) or DF x K
    (put). A price on or outside them has no implied volatility."""
    return df * _intrinsic(right, fwd, strike), df * (fwd if right == "call" else strike)


def carry(fwd: float, spot: float, t: float) -> float:
    """c = ln(F/S) / T, the annual carry the forward implies."""
    return math.log(fwd / spot) / t


def implied_q(rate: float | None, fwd: float, spot: float, t: float) -> float | None:
    """The implied dividend yield q = r - c; unknown without a rate."""
    return None if rate is None else rate - carry(fwd, spot, t)


def expected_move(spot: float, sigma: float, t: float) -> tuple[float, float]:
    """The lognormal one-sigma band S e^(-sigma sqrt T), S e^(+sigma sqrt T) (not the symmetric S +/- S sigma sqrt T)."""
    sd = sigma * math.sqrt(t)
    return spot * math.exp(-sd), spot * math.exp(sd)


@dataclass(frozen=True)
class IvResult:
    """sigma (annual fraction) or None with `reason`; `price_error` = |model - target| at sigma; `iterations`."""

    sigma: float | None
    reason: str | None = None
    price_error: float | None = None
    iterations: int = 0


def _finite_positive(*xs: float | None) -> bool:
    return all(x is not None and math.isfinite(x) and x > 0 for x in xs)


def implied_vol(right: Right, target: float, fwd: float, strike: float, t: float, df: float | None) -> IvResult:
    """The volatility that reproduces `target`, or unknown with the reason: bad inputs, a price on or outside the
    no-arbitrage bounds, or outside the [SIGMA_LO, SIGMA_HI] bracket. An in-the-money option is solved on its
    out-of-the-money twin through parity (C - P = DF (F - K)), whose time value is the same and is not swamped by
    the intrinsic value."""
    if not _finite_positive(fwd, strike, t, df) or target is None or not math.isfinite(target):
        return IvResult(None, "forward, strike, time and discount factor must be known and positive")
    if df > MAX_DF:  # type: ignore[operator]
        return IvResult(None, f"discount factor {df:g} is above {MAX_DF:g}: an input error")
    lo, hi = bounds(right, fwd, strike, df)  # type: ignore[arg-type]
    if target <= lo:
        return IvResult(None, f"price {target:g} is at or below the no-arbitrage lower bound {lo:g}")
    if target >= hi:
        return IvResult(None, f"price {target:g} is at or above the no-arbitrage upper bound {hi:g}")
    side: Right = right
    p = target
    if _intrinsic(right, fwd, strike) > 0:  # in the money: the other side at the same strike
        side = "put" if right == "call" else "call"
        p = target - df * (fwd - strike) if right == "call" else target + df * (fwd - strike)  # type: ignore[operator]
    tol = PRICE_TOL * p  # relative to the time value: an absolute tolerance would be loose on a deep wing
    a, b = SIGMA_LO, SIGMA_HI
    ga = price(side, fwd, strike, t, df, a) - p  # type: ignore[arg-type]
    if ga > 0:
        return IvResult(None, f"price {target:g} is below the value at sigma {SIGMA_LO:g} (outside the bracket)")
    if price(side, fwd, strike, t, df, b) - p < 0:  # type: ignore[arg-type]
        return IvResult(None, f"price {target:g} is above the value at sigma {SIGMA_HI:g} (outside the bracket)")
    # start: the Brenner-Subrahmanyam ATM approximation sigma ~ sqrt(2 pi / T) x price / (DF F), kept in the bracket
    s = min(b, max(a, math.sqrt(2 * math.pi / t) * p / (df * fwd)))  # type: ignore[operator]
    for i in range(1, MAX_ITER + 1):
        g = price(side, fwd, strike, t, df, s) - p  # type: ignore[arg-type]
        if abs(g) <= tol:
            return IvResult(s, None, abs(g), i)
        if g > 0:
            b = s
        else:
            a = s
        if b - a < SIGMA_TOL:
            return IvResult(s, None, abs(g), i)
        v = vega(fwd, strike, t, df, s)  # type: ignore[arg-type]
        nxt = s - g / v if v > 0 else math.nan
        s = nxt if a < nxt < b else (a + b) / 2  # Newton when it stays inside the bracket, else bisection
    return IvResult(None, f"no convergence in {MAX_ITER} steps")

"""What one arbitrage-free SVI slice (fincalc/svi.py) gives (#386, P3.3): the risk-neutral density and probabilities,
25-delta risk reversal and butterfly, constant-maturity interpolation, smile-consistent greeks, Bakshi-Kapadia-Madan
implied moments and the P&L at expiry of a set of legs under the smile's density.

    k = ln(K/F), w = w(k) from the slice, d-(k) = -k/sqrt(w) - sqrt(w)/2, d+ = d- + sqrt(w)
    density of k = ln(F_T/F):  p(k) = g(k) / sqrt(2 pi w) exp(-d-^2/2)            (Gatheral and Jacquier 2014, 2.2)
    P_Q(F_T < K) = e^(rT) dP/dK = N(-d-) + n(d-) w'(k) / (2 sqrt w)              (Breeden and Litzenberger 1978)
    RR25 = sigma(25d put) - sigma(25d call),  BF25 = [sigma(25d put) + sigma(25d call)]/2 - sigma_ATM
    deltas are forward deltas N(d+) (call) and N(d+) - 1 (put) without the discount factor; ATM = the forward (k = 0)

Every input slice must have passed svi.check (an unchecked or refused slice has no density); a quantity that cannot
be found (a delta strike outside the check grid, an expiry outside the fitted ones) is None with the reason.
Pure functions, no I/O; the live card does not use this (switching is the horse race's decision, #377).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

from finresearch.fincalc import black76 as b76
from finresearch.fincalc import options as o
from finresearch.fincalc import svi
from finresearch.fincalc.svi import Raw

_N = NormalDist()
DELTA = 0.25
DELTA_TOL = 1e-13  # the delta strike's bisection stops when the bracket in k is narrower than this


def sigma(p: Raw, k: float, t: float) -> float:
    return math.sqrt(float(svi.w(p, k)) / t)


def _dminus(p: Raw, k: float) -> tuple[float, float]:
    sw = math.sqrt(float(svi.w(p, k)))
    return -k / sw - sw / 2, sw


def density(p: Raw, k: float) -> float:
    """The risk-neutral density of k = ln(F_T/F) at k (the density of F_T at K is density / K)."""
    dm, sw = _dminus(p, k)
    return float(svi.g(p, k)) / (math.sqrt(2 * math.pi) * sw) * math.exp(-dm * dm / 2)


def cdf(p: Raw, k: float) -> float:
    """P_Q(F_T < F e^k): the smile-consistent digital (the flat-volatility N(-d-) plus the skew's vega term)."""
    dm, sw = _dminus(p, k)
    return _N.cdf(-dm) + _N.pdf(dm) * float(svi.dw(p, k)) / (2 * sw)


def forward_delta(p: Raw, k: float) -> float:
    """N(d+) at k with the smile's own volatility (a call's undiscounted forward delta)."""
    dm, sw = _dminus(p, k)
    return _N.cdf(dm + sw)


def delta_strike(p: Raw, target: float) -> float | None:
    """k where the call's forward delta N(d+) equals `target` (0.25: the 25-delta call; 0.75: the 25-delta put),
    searching outward from the forward on the check grid's range; None when it is not inside it."""
    sd = math.sqrt(float(svi.w(p, 0.0)))
    side = 1.0 if target < forward_delta(p, 0.0) else -1.0
    lo, step = 0.0, 0.05 * sd
    hi = lo + side * step
    while abs(hi) <= svi.K_WIDE:
        if (forward_delta(p, hi) - target) * side <= 0:
            break
        lo, hi = hi, hi + side * step
    else:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if (forward_delta(p, mid) - target) * side > 0:
            lo = mid
        else:
            hi = mid
        if abs(hi - lo) < DELTA_TOL:
            break
    return (lo + hi) / 2


@dataclass(frozen=True)
class RrBf:
    atm: float | None  # sigma at k = 0
    put25: float | None
    call25: float | None
    rr: float | None
    bf: float | None
    k_put: float | None = None
    k_call: float | None = None
    reason: str | None = None


def rr_bf(p: Raw, t: float) -> RrBf:
    atm = sigma(p, 0.0, t)
    kc, kp = delta_strike(p, DELTA), delta_strike(p, 1 - DELTA)
    if kc is None or kp is None:
        return RrBf(
            atm, None, None, None, None, kp, kc, "a 25-delta strike is outside the smile's check grid"
        )
    put, call = sigma(p, kp, t), sigma(p, kc, t)
    return RrBf(atm, put, call, put - call, (put + call) / 2 - atm, kp, kc)


# --------------------------------------------------------------------------- constant maturity
def constant_maturity(slices: list[tuple[float, Raw]], t_target: float) -> RrBf:
    """ATM, 25-delta put and call volatilities at `t_target`: total variance at each fixed delta, linear in T between
    the two fitted expiries around it (T on one clock for every input: business years for the issue's 30-day
    business-time figure). No extrapolation: a target outside the fitted expiries is unknown."""
    rows = sorted(slices, key=lambda x: x[0])
    if not rows or not rows[0][0] <= t_target <= rows[-1][0]:
        return RrBf(None, None, None, None, None, reason="the target maturity is outside the fitted expiries "
                    "(no extrapolation)")  # fmt: skip
    i = next(j for j, (t, _) in enumerate(rows) if t >= t_target)
    pair = [rows[i]] if rows[i][0] == t_target or i == 0 else [rows[i - 1], rows[i]]
    pts = []
    for t, p in pair:
        x = rr_bf(p, t)
        if x.put25 is None or x.call25 is None:
            return RrBf(None, None, None, None, None, reason=f"T = {t:.4f}: {x.reason}")
        pts.append((t, [x.atm**2 * t, x.put25**2 * t, x.call25**2 * t]))  # type: ignore[operator]
    if len(pts) == 1:
        ws = pts[0][1]
    else:
        (t1, w1), (t2, w2) = pts
        u = (t_target - t1) / (t2 - t1)
        ws = [a + (b - a) * u for a, b in zip(w1, w2, strict=True)]
    atm, put, call = (math.sqrt(v / t_target) for v in ws)
    return RrBf(atm, put, call, put - call, (put + call) / 2 - atm)


# --------------------------------------------------------------------------- greeks
def vanna(fwd: float, strike: float, t: float, df: float, sig: float) -> float:
    """d(forward delta)/d sigma = d vega / dF = -DF n(d1) d2 / sigma (the issue's spot form has e^(-qT) for DF)."""
    d1 = (math.log(fwd / strike) + sig * sig * t / 2) / (sig * math.sqrt(t))
    return -df * _N.pdf(d1) * (d1 - sig * math.sqrt(t)) / sig


def volga(fwd: float, strike: float, t: float, df: float, sig: float) -> float:
    """d vega / d sigma = vega d1 d2 / sigma."""
    sd = sig * math.sqrt(t)
    d1 = (math.log(fwd / strike) + sd * sd / 2) / sd
    return b76.vega(fwd, strike, t, df, sig) * d1 * (d1 - sd) / sig


def smile_delta(right: b76.Right, fwd: float, strike: float, t: float, df: float, p: Raw) -> float:
    """dV/dF when the smile moves with the forward (sticky moneyness: sigma depends on k = ln(K/F)):
    delta_B76 + vega x d sigma/dF with d sigma/dF = -sigma'(k)/F and sigma'(k) = w'(k) / (2 sigma T)."""
    k = math.log(strike / fwd)
    sig = sigma(p, k, t)
    dsig = float(svi.dw(p, k)) / (2 * sig * t)
    return b76.forward_delta(right, fwd, strike, t, df, sig) - b76.vega(fwd, strike, t, df, sig) * dsig / fwd


@dataclass(frozen=True)
class Value:
    value: float | None
    reason: str | None = None


def hull_white_delta(delta: float, vega: float, spot: float, t: float,
                     coef: tuple[float, float, float] | None) -> Value:  # fmt: skip
    """Hull and White's (2017, J. Banking & Finance 82, 180-190) minimum-variance delta
    delta + vega / (S sqrt T) (a + b delta + c delta^2). (a, b, c) are regression estimates from the underlying's own
    history; without them it is unknown (none are estimated here)."""
    if coef is None:
        return Value(None, "Hull-White coefficients (a, b, c) not estimated")
    a, b, c = coef
    return Value(delta + vega / (spot * math.sqrt(t)) * (a + b * delta + c * delta * delta))


# --------------------------------------------------------------------------- implied moments (display only)
BKM_POINTS = 20001  # trapezoid nodes on [-K_WIDE, K_WIDE] for the spanning integrals


@dataclass(frozen=True)
class Moments:
    mean: float  # of ln(F_T/F)
    var: float
    skew: float
    kurt: float  # not excess: 3 for a normal


def bkm_moments(p: Raw, t: float) -> Moments:
    """Bakshi, Kapadia and Madan (2003, Review of Financial Studies 16(1), 101-143): the moments of R = ln(F_T/F)
    spanned by out-of-the-money options. Under the forward measure, for H(F_T) with H(F) = 0,
    E[H] = H'(F) E[F_T - F] + int_0^F H''(K) P(K) dK + int_F^inf H''(K) C(K) dK and E[F_T - F] = 0; with F = 1 and
    K = e^k, H = R^n gives H''(K) K = (n(n-1) k^(n-2) - n k^(n-1)) e^(-k). Undiscounted prices, so no rate enters.
    For display: P2.1 logs no forecast from it."""
    ks = np.linspace(-svi.K_WIDE, svi.K_WIDE, BKM_POINTS)
    otm = np.array([b76.price("put" if k < 0 else "call", 1.0, math.exp(k), t, 1.0, sigma(p, float(k), t))
                    for k in ks])  # fmt: skip
    base = otm * np.exp(-ks)

    def span(n: int) -> float:
        h2k = n * (n - 1) * ks ** (n - 2) - n * ks ** (n - 1) if n > 1 else -np.ones_like(ks)
        return float(np.trapezoid(h2k * base, ks))

    mu, v, w3, x4 = span(1), span(2), span(3), span(4)
    var = v - mu * mu
    return Moments(mu, var, (w3 - 3 * mu * v + 2 * mu**3) / var**1.5,
                   (x4 - 4 * mu * w3 + 6 * mu * mu * v - 3 * mu**4) / var**2)  # fmt: skip


# --------------------------------------------------------------------------- P&L at expiry under the smile's density
# As fincalc.options.outcome with the lognormal replaced by the smile's density: S_T = F e^k with F = spot e^(rT),
# k on a midpoint grid of OUT_STEPS cells over +/- OUT_SD ATM standard deviations sqrt(w(0)) (the mass beyond is
# dropped and the rest renormalised, as options.lognormal_grid's +/- 6 sigma); PoP from the smile's exact CDF.
OUT_SD = 12.0
OUT_STEPS = 4000


def _grid(p: Raw, steps: int) -> list[tuple[float, float]]:
    """(k, probability mass) on the outcome grid."""
    half = OUT_SD * math.sqrt(float(svi.w(p, 0.0)))
    dk = 2 * half / steps
    ks = -half + (np.arange(steps) + 0.5) * dk
    sw = np.sqrt(np.asarray(svi.w(p, ks)))
    dm = -ks / sw - sw / 2
    mass = (
        np.asarray(svi.g(p, ks)) / (math.sqrt(2 * math.pi) * sw) * np.exp(-dm * dm / 2) * dk
    )  # density(p, k) dk
    return list(zip(ks.tolist(), mass.tolist(), strict=True))


def outcome(legs, spot: float, t: float, rate: float, p: Raw, entry_cost: float = 0.0, exit_cost=None,
            steps: int = OUT_STEPS) -> o.Outcome:  # fmt: skip
    """PoP and EV (discounted at `rate`) of holding `legs` to expiry with S_T distributed as the smile implies around
    the forward spot e^(rT); `vol` in the result is the smile's ATM volatility (for reference only)."""
    fwd = spot * math.exp(rate * t)
    net = sum(x.premium * x.qty for x in legs if x.right != "future")
    grid = _grid(p, steps)
    total = sum(m for _, m in grid)
    e_gross = e_exit = 0.0
    dist = []
    for k, m in grid:
        price = fwd * math.exp(k)
        gross = o.payoff(legs, price) + net
        xc = exit_cost(price) if exit_cost else 0.0
        e_gross, e_exit = e_gross + gross * m, e_exit + xc * m
        dist.append((gross - net - entry_cost - xc, m / total))
    df = math.exp(-rate * t)

    def pnl(x: float) -> float:
        return o.payoff(legs, x) - entry_cost - (exit_cost(x) if exit_cost else 0.0)

    def below(x: float) -> float:
        return 0.0 if x <= 0 else 1.0 if x == math.inf else cdf(p, math.log(x / fwd))

    pop = sum(below(hi) - below(lo) for lo, hi in o.profit_intervals(pnl, [x.strike for x in legs]))
    ev_gross = df * e_gross / total - net
    q = o.weighted_quantiles(dist, (0.1, 0.25, 0.5, 0.75, 0.9))
    return o.Outcome(sigma(p, 0.0, t), rate, pop, ev_gross - entry_cost - df * e_exit / total, ev_gross,
                     e_exit / total, dict(zip(("p10", "p25", "p50", "p75", "p90"), q, strict=True)))  # fmt: skip


def pnl_distribution(legs, spot: float, t: float, rate: float, p: Raw, entry_cost: float = 0.0, exit_cost=None,
                     steps: int = 2000) -> list[tuple[float, float]]:  # fmt: skip
    """(P&L after costs, probability) pairs at expiry under the smile's density (options.pnl_distribution's twin)."""
    fwd = spot * math.exp(rate * t)
    grid = _grid(p, steps)
    total = sum(m for _, m in grid)
    out = []
    for k, m in grid:
        price = fwd * math.exp(k)
        out.append((o.payoff(legs, price) - entry_cost - (exit_cost(price) if exit_cost else 0.0), m / total))
    return out

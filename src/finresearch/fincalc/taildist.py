"""Tail-aware physical (real-world) distributions for an option structure's P&L at expiry (P3.4, #387).

Two models, both scaled to the option market's ATM IV so that only the SHAPE comes from history:
  M2  IV-scaled empirical terminal model: z_i = [ln(S_T,i / F_i) + sigma_i^2 T_i / 2] / (sigma_i sqrt(T_i)) per past
      expiry (sigma_i the ATM IV at entry); a new trade has S_T = F exp(-kappa sigma^2 T / 2 + sigma sqrt(T) z*) with
      kappa = E[z^2]; the point estimate is exact on the empirical z distribution, the EV interval comes from a
      stationary block bootstrap over expiries (Politis and Romano 1994, JASA 89, 1303-1313); an optional GPD
      peaks-over-threshold tail (Pickands 1975; McNeil and Frey 2000, J. Empirical Finance 7, 271-300).
  M3  filtered historical simulation (Barone-Adesi, Giannopoulos and Vosper 1999, J. Futures Markets 19, 583-602) on a
      GJR-GARCH(1,1) (Glosten, Jagannathan and Runkle 1993, J. Finance 48, 1779-1801) or EWMA (RiskMetrics 1996,
      lambda 0.94) filter, rescaled so that E[sum r^2] = kappa sigma_IV^2 T.

Plus: expected shortfall on a weighted sample, the robust EV gate (maxmin over validated models, Gilboa and
Schmeidler 1989, J. Math. Econ. 18, 141-153; never more permissive than the gate it wraps), drawdown by block
bootstrap (DaR/CDaR: Chekhlov, Uryasev and Zabarankin 2005, IJTAF 8, 13-58), VRP and Kelly with estimation error.
Library only: the live card's PoP/EV and gates are unchanged (they change only through the horse race, #377).
Every registered number is a module constant, hashed by horse-race addendum A5.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from finresearch.fincalc import options as o

DOTS = [(i + 0.5) / 20 for i in range(20)]
ES_ALPHA = 0.025  # ES97.5 (the issue's; Basel FRTB's level)


# ----------------------------------------------------------------------------------------------- the z statistic
def terminal_z(settle: float | None, fwd: float | None, sigma: float | None, t: float | None) -> float | None:
    """z = [ln(S_T / F) + sigma^2 T / 2] / (sigma sqrt(T)); None when any input is unknown or not positive."""
    if not settle or not fwd or not sigma or not t or settle <= 0 or fwd <= 0 or sigma <= 0 or t <= 0:
        return None
    return (math.log(settle / fwd) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))


def kappa(zs: Sequence[float]) -> float | None:
    """kappa = E[z^2] (1 for a lognormal at the right IV); None on an empty sample."""
    return float(np.mean(np.square(zs))) if len(zs) else None


def iv_scaled_terminal(fwd: float, sigma: float, t: float, z: np.ndarray, kap: float) -> np.ndarray:
    """S_T = F exp(-kappa sigma^2 T / 2 + sigma sqrt(T) z)."""
    return fwd * np.exp(-0.5 * kap * sigma * sigma * t + sigma * math.sqrt(t) * np.asarray(z, float))


# ------------------------------------------------------------------------------------------- expected shortfall
def expected_shortfall(x: Sequence[float], w: Sequence[float] | None = None, alpha: float = ES_ALPHA) -> float:
    """ES_alpha as a positive loss: -(1/alpha)[sum_{i<k} w_i x_(i) + (alpha - sum_{i<k} w_i) x_(k)] on the sorted
    values, k the first index whose cumulative weight reaches alpha (weights normalised to 1)."""
    xs = np.asarray(x, float)
    ws = np.full(len(xs), 1.0 / len(xs)) if w is None else np.asarray(w, float) / float(np.sum(w))
    order = np.argsort(xs, kind="stable")
    xs, ws = xs[order], ws[order]
    acc = total = 0.0
    for xi, wi in zip(xs, ws, strict=True):
        take = min(wi, alpha - acc)
        total += take * xi
        acc += take
        if acc >= alpha - 1e-15:
            break
    return -total / alpha


# --------------------------------------------------------------------------- outcome from a weighted sample
@dataclass(frozen=True)
class SampleOutcome:
    pop: float  # P(P&L after costs > 0)
    ev: float  # DF x E[gross] - net premium - entry costs - DF x E[exit costs] (fincalc.options.outcome's)
    dots: list[float]  # the 20 P&L quantiles at (i + 0.5) / 20 (fincalc.options.weighted_quantiles)
    es: float  # ES97.5 of the undiscounted P&L, a positive loss
    n: int


def sample_outcome(legs: Sequence[o.Leg], s_t: Sequence[float], t: float, rate: float, entry_cost: float = 0.0,
                   exit_cost: Callable[[float], float] | None = None,
                   weights: Sequence[float] | None = None) -> SampleOutcome:  # fmt: skip
    """PoP, EV, dots and ES of holding `legs` to expiry over terminal prices `s_t` with `weights` (equal if None),
    on fincalc.options.outcome's conventions so that a lognormal grid reproduces it."""
    st = np.asarray(s_t, float)
    w = np.full(len(st), 1.0 / len(st)) if weights is None else np.asarray(weights, float) / float(np.sum(weights))
    net = sum(x.premium * x.qty for x in legs if x.right != "future")
    gross = np.zeros(len(st))
    for x in legs:
        if x.right == "future":
            gross += x.qty * (st - x.strike)
        else:
            gross += x.qty * (np.maximum(st - x.strike, 0) if x.right == "call" else np.maximum(x.strike - st, 0))
    xc = np.array([exit_cost(float(p)) for p in st]) if exit_cost else np.zeros(len(st))
    pnl = gross - net - entry_cost - xc
    df = math.exp(-rate * t)
    ev = df * float(w @ gross) - net - entry_cost - df * float(w @ xc)
    dist = list(zip(pnl.tolist(), w.tolist(), strict=True))
    return SampleOutcome(float(w[pnl > 0].sum()), ev, o.weighted_quantiles(dist, DOTS),
                         expected_shortfall(pnl, w), len(st))  # fmt: skip


# ------------------------------------------------------------------------------------- the stationary bootstrap
def stationary_bootstrap(n: int, size: int, mean_block: float, rng: np.random.Generator) -> np.ndarray:
    """`size` indices into a series of length n by Politis and Romano's stationary bootstrap: a run starts at a
    uniform index and continues to i + 1 (mod n) with probability 1 - 1/mean_block, so block lengths are geometric
    with mean `mean_block`."""
    if n < 1 or size < 1 or mean_block < 1:
        raise ValueError("a series, a size and a mean block length of at least 1 are required")
    starts = rng.integers(0, n, size)
    restart = rng.random(size) < 1.0 / mean_block
    restart[0] = True
    out = np.empty(size, dtype=np.int64)
    cur = 0
    for i in range(size):
        cur = starts[i] if restart[i] else (cur + 1) % n
        out[i] = cur
    return out


# ------------------------------------------------------------------------------------------- the GPD tail
GPD_THETA_GRID = 4000  # Grimshaw's profile likelihood is searched on this grid of theta = xi / beta, then refined


@dataclass(frozen=True)
class GpdFit:
    xi: float
    beta: float
    loglik: float
    n: int


def _gpd_profile(theta: float, x: np.ndarray) -> float:
    """Grimshaw (1993, Technometrics 35, 185-191): for theta = xi / beta the MLE of xi is mean(log(1 + theta x)), and
    the log-likelihood is -n log(xi / theta) - n (1 + xi); theta -> 0 is the exponential limit."""
    n = len(x)
    if abs(theta) < 1e-12:
        return -n * math.log(float(np.mean(x))) - n
    a = 1 + theta * x
    if np.any(a <= 0):
        return -math.inf
    xi = float(np.mean(np.log(a)))
    # xi <= -1: the likelihood is unbounded as theta -> -1 / max x (no MLE; Smith 1985, Biometrika 72, 67-90), so
    # only the interior local maximum with xi > -1 counts (scipy's genpareto.fit lands on the same one)
    return -n * math.log(xi / theta) - n * (1 + xi) if xi / theta > 0 and xi > -1 else -math.inf


def _golden_max(f: Callable[[float], float], lo: float, hi: float, tol: float = 1e-12) -> float:
    g = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = f(c), f(d)
    while abs(b - a) > tol * max(1.0, abs(a) + abs(b)):
        if fc >= fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = f(d)
    return (a + b) / 2


def gpd_fit(excess: Sequence[float]) -> GpdFit:
    """Maximum-likelihood GPD(xi, beta) of positive excesses over a threshold (location 0), by Grimshaw's profile
    likelihood: a grid over theta in (-1 / max x, 50 / mean x) then golden-section refinement."""
    x = np.asarray(excess, float)
    if len(x) < 2 or np.any(x <= 0):
        raise ValueError("at least two positive excesses are required")
    lo, hi = -1.0 / float(x.max()) * (1 - 1e-9), 50.0 / float(np.mean(x))
    grid = np.concatenate([lo * (1 - np.geomspace(1e-9, 1, GPD_THETA_GRID // 2)),
                           hi * np.geomspace(1e-9, 1, GPD_THETA_GRID // 2)])  # fmt: skip
    vals = np.array([_gpd_profile(t, x) for t in grid])
    order = np.argsort(grid)
    grid, vals = grid[order], vals[order]
    i = int(np.argmax(vals))
    theta = _golden_max(lambda t: _gpd_profile(t, x), grid[max(i - 1, 0)], grid[min(i + 1, len(grid) - 1)])
    if abs(theta) < 1e-12:
        return GpdFit(0.0, float(np.mean(x)), _gpd_profile(0.0, x), len(x))
    xi = float(np.mean(np.log1p(theta * x)))
    return GpdFit(xi, float(xi / theta), _gpd_profile(theta, x), len(x))


@dataclass(frozen=True)
class GpdTail:
    threshold: float  # u: the q-quantile of |z|
    q: float
    xi: float
    beta: float
    n_exceed: int


def gpd_tail(z: Sequence[float], q: float, min_exceed: int) -> GpdTail | None:
    """A GPD fitted to |z| - u over |z| > u, u the q-quantile of |z| (numpy's linear rule); None with fewer than
    `min_exceed` exceedances (too few to fit a tail: none is better than noise)."""
    a = np.abs(np.asarray(z, float))
    u = float(np.quantile(a, q))
    e = a[a > u] - u
    if len(e) < min_exceed:
        return None
    f = gpd_fit(e)
    return GpdTail(u, q, f.xi, f.beta, len(e))

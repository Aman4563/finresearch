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
def expected_shortfall(
    x: Sequence[float], w: Sequence[float] | None = None, alpha: float = ES_ALPHA
) -> float:
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
    w = (
        np.full(len(st), 1.0 / len(st))
        if weights is None
        else np.asarray(weights, float) / float(np.sum(weights))
    )
    net = sum(x.premium * x.qty for x in legs if x.right != "future")
    gross = np.zeros(len(st))
    for x in legs:
        if x.right == "future":
            gross += x.qty * (st - x.strike)
        else:
            gross += x.qty * (
                np.maximum(st - x.strike, 0) if x.right == "call" else np.maximum(x.strike - st, 0)
            )
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
GPD_THETA_GRID = (
    4000  # Grimshaw's profile likelihood is searched on this grid of theta = xi / beta, then refined
)


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


# ------------------------------------------------------------------------------------------- filters
EWMA_LAMBDA = 0.94  # RiskMetrics (1996) daily decay; NSE's margin volatility uses 0.995
BACKCAST_TAU, BACKCAST_DECAY = (
    75,
    0.94,
)  # arch's (K. Sheppard) variance backcast: EWMA weights over the first 75
GJR_WINDOW = 1000  # the last 1,000 daily returns (about four years) on or before the fit date
GJR_MIN_RETURNS = 250  # fewer: no fit (unknown)
GJR_START = (0.05, 0.05, 0.85)  # alpha, gamma, beta; omega = var x (1 - alpha - gamma / 2 - beta)
NM_STEP, NM_XATOL, NM_FATOL, NM_MAXFEV, NM_RESTARTS = 0.1, 1e-9, 1e-10, 4000, 2


@dataclass(frozen=True)
class Minimum:
    x: list[float]
    fun: float
    converged: bool
    nfev: int


def nelder_mead(f: Callable[[np.ndarray], float], x0: Sequence[float], step: Sequence[float],
                xatol: float = NM_XATOL, fatol: float = NM_FATOL, maxfev: int = NM_MAXFEV) -> Minimum:  # fmt: skip
    """Nelder and Mead (1965) with the standard coefficients (reflect 1, expand 2, contract 1/2, shrink 1/2);
    converged when the simplex spans < xatol in x and < fatol in f."""
    n = len(x0)
    sim = np.array(
        [np.asarray(x0, float)] + [np.asarray(x0, float) + np.eye(n)[i] * step[i] for i in range(n)]
    )
    fs = np.array([f(x) for x in sim])
    nfev = n + 1
    while nfev < maxfev:
        order = np.argsort(fs)
        sim, fs = sim[order], fs[order]
        if np.max(np.abs(sim[1:] - sim[0])) <= xatol and np.max(np.abs(fs[1:] - fs[0])) <= fatol:
            return Minimum(sim[0].tolist(), float(fs[0]), True, nfev)
        c = sim[:-1].mean(axis=0)
        xr = c + (c - sim[-1])
        fr = f(xr)
        nfev += 1
        if fr < fs[0]:
            xe = c + 2 * (c - sim[-1])
            fe = f(xe)
            nfev += 1
            sim[-1], fs[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < fs[-2]:
            sim[-1], fs[-1] = xr, fr
        else:
            xc = c + 0.5 * (xr - c) if fr < fs[-1] else c + 0.5 * (sim[-1] - c)
            fc = f(xc)
            nfev += 1
            if fc < min(fr, fs[-1]):
                sim[-1], fs[-1] = xc, fc
            else:
                sim[1:] = sim[0] + 0.5 * (sim[1:] - sim[0])
                fs[1:] = [f(x) for x in sim[1:]]
                nfev += n
    i = int(np.argmin(fs))
    return Minimum(sim[i].tolist(), float(fs[i]), False, nfev)


def backcast(r: np.ndarray) -> float:
    """arch's starting variance: sum of w_i r_i^2 over the first min(75, n) returns, w_i proportional to 0.94^i."""
    tau = min(BACKCAST_TAU, len(r))
    w = BACKCAST_DECAY ** np.arange(tau)
    return float(np.sum(w / w.sum() * np.asarray(r[:tau], float) ** 2))


def ewma_variance(r: Sequence[float], lam: float = EWMA_LAMBDA, start: float | None = None) -> list[float]:
    """sigma^2_t = lam sigma^2_{t-1} + (1 - lam) r^2_{t-1}: the variance of each return given the earlier ones, plus
    the next session's forecast (len(r) + 1 values); `start` defaults to the backcast."""
    s = backcast(np.asarray(r, float)) if start is None else start
    out = [s]
    for x in r:
        s = lam * s + (1 - lam) * x * x
        out.append(s)
    return out


@dataclass(frozen=True)
class GjrParams:
    omega: float
    alpha: float
    gamma: float
    beta: float

    def step(self, s2: float, r: float) -> float:
        return self.omega + (self.alpha + self.gamma * (r < 0)) * r * r + self.beta * s2


@dataclass(frozen=True)
class Filtered:
    sigma2: list[float]  # the conditional variance of each return
    z: list[float]  # standardised residuals r_t / sigma_t
    next_sigma2: float  # the forecast for the session after the last return


def gjr_filter(r: Sequence[float], p: GjrParams) -> Filtered:
    """sigma^2_0 = omega + (alpha + gamma / 2 + beta) x backcast (arch's), then the GJR recursion; zero mean."""
    bc = backcast(np.asarray(r, float))
    s = p.omega + (p.alpha + p.gamma / 2 + p.beta) * bc
    s2, z = [], []
    for x in r:
        s2.append(s)
        z.append(x / math.sqrt(s))
        s = p.step(s, x)
    return Filtered(s2, z, s)


def _gjr_nll(r: list[float], bc: float, p: GjrParams) -> float:
    if p.omega <= 0 or p.alpha < 0 or p.gamma < 0 or p.beta < 0 or p.alpha + p.gamma / 2 + p.beta >= 1:
        return math.inf
    s = p.omega + (p.alpha + p.gamma / 2 + p.beta) * bc
    tot = 0.0
    for x in r:
        tot += math.log(s) + x * x / s
        s = p.omega + (p.alpha + p.gamma * (x < 0)) * x * x + p.beta * s
    return 0.5 * (len(r) * math.log(2 * math.pi) + tot)


@dataclass(frozen=True)
class GjrFit:
    params: GjrParams | None
    loglik: float | None
    n: int
    reason: str | None = None


def gjr_fit(returns: Sequence[float], window: int = GJR_WINDOW, min_returns: int = GJR_MIN_RETURNS) -> GjrFit:
    """Gaussian quasi-maximum likelihood of a zero-mean GJR-GARCH(1,1) on the last `window` returns (omega > 0,
    alpha, gamma, beta >= 0, alpha + gamma / 2 + beta < 1), by Nelder-Mead from GJR_START restarted NM_RESTARTS
    times; unknown (params None, a reason) when too short or not converged. Never a fallback."""
    r = [float(x) for x in list(returns)[-window:]]
    if len(r) < min_returns:
        return GjrFit(None, None, len(r), f"{len(r)} returns, fewer than {min_returns}")
    var, bc = float(np.var(r)), backcast(np.asarray(r))
    if var <= 0:
        return GjrFit(None, None, len(r), "zero variance")
    a, g, b = GJR_START
    x0 = [1 - a - g / 2 - b, a, g, b]  # omega / var, alpha, gamma, beta

    def nll(x: np.ndarray) -> float:
        return _gjr_nll(r, bc, GjrParams(x[0] * var, x[1], x[2], x[3]))

    res = None
    for _ in range(NM_RESTARTS + 1):
        res = nelder_mead(nll, x0, [NM_STEP * max(abs(v), 0.01) for v in x0])
        x0 = res.x
    assert res is not None
    if not res.converged or not math.isfinite(res.fun):
        return GjrFit(None, None, len(r), f"not converged after {res.nfev} evaluations")
    x = res.x
    return GjrFit(GjrParams(x[0] * var, x[1], x[2], x[3]), -res.fun, len(r))


# ------------------------------------------------------------------------------------------- M2: IV-scaled empirical
MIN_Z = 30  # fewer past expiries in the bucket: the model is unknown
BLOCK_MEAN = 3.0  # expected stationary-bootstrap block, in expiries (the drawdown block of the issue)
EV_BOOT = 500  # bootstrap replicates of the z series for the EV interval
EV_LEVEL = 0.90  # two-sided: the lower bound is the 5th percentile (the gate's 90 % lower bound)
PATHS = 20_000  # Monte Carlo draws where the model is sampled (a GPD tail; FHS)


def sample_z(zs: Sequence[float], size: int, rng: np.random.Generator, mean_block: float = BLOCK_MEAN,
             tail: GpdTail | None = None) -> np.ndarray:  # fmt: skip
    """`size` draws of z by the stationary bootstrap over expiries; with a GPD tail every draw beyond the threshold
    keeps its sign and takes |z| = u + a GPD draw (inverse CDF: beta ((1 - U)^-xi - 1) / xi)."""
    z = np.asarray(zs, float)[stationary_bootstrap(len(zs), size, mean_block, rng)]
    if tail is None:
        return z
    out = np.abs(z) > tail.threshold
    u = rng.random(int(out.sum()))
    exc = (tail.beta * -np.log1p(-u) if abs(tail.xi) < 1e-12
           else tail.beta * (np.power(1 - u, -tail.xi) - 1) / tail.xi)  # fmt: skip
    z[out] = np.sign(z[out]) * (tail.threshold + exc)
    return z


@dataclass(frozen=True)
class ModelForecast:
    outcome: SampleOutcome | None  # None = unknown, with `reason`
    ev_lo: float | None  # the EV interval at EV_LEVEL (bootstrap over expiries); None when not estimated
    ev_hi: float | None
    kappa: float | None
    n: int  # z values (M2) or returns (M3) behind it
    reason: str | None = None


def empirical_forecast(legs: Sequence[o.Leg], fwd: float, sigma: float | None, t: float, rate: float,
                       entry_cost: float, exit_cost: Callable[[float], float] | None, zs: Sequence[float], *,
                       rng: np.random.Generator, min_z: int = MIN_Z, mean_block: float = BLOCK_MEAN,
                       boot: int = EV_BOOT, level: float = EV_LEVEL, tail: GpdTail | None = None,
                       paths: int = PATHS) -> ModelForecast:  # fmt: skip
    """M2. Without a tail the point estimates are exact on the empirical z distribution (the limit of drawing
    z* by the bootstrap); with one they are a Monte Carlo of `paths` draws. The EV interval re-estimates kappa and
    the EV on `boot` stationary-bootstrap replicates of the z series (length n each)."""
    z = np.asarray(zs, float)
    if not sigma or sigma <= 0:
        return ModelForecast(None, None, None, None, len(z), "no ATM IV")
    if len(z) < min_z:
        return ModelForecast(None, None, None, None, len(z), f"{len(z)} z values, fewer than {min_z}")
    k = kappa(z)
    assert k is not None
    draws = z if tail is None else sample_z(z, paths, rng, mean_block, tail)
    point = sample_outcome(legs, iv_scaled_terminal(fwd, sigma, t, draws, k), t, rate, entry_cost, exit_cost)
    evs = []
    for _ in range(boot):
        zb = z[stationary_bootstrap(len(z), len(z), mean_block, rng)]
        kb = kappa(zb)
        evs.append(sample_outcome(legs, iv_scaled_terminal(fwd, sigma, t, zb, kb), t, rate, entry_cost,  # type: ignore[arg-type]
                                  exit_cost).ev)  # fmt: skip
    lo, hi = (
        (float(np.quantile(evs, (1 - level) / 2)), float(np.quantile(evs, (1 + level) / 2)))
        if evs
        else (None, None)
    )
    return ModelForecast(point, lo, hi, k, len(z))

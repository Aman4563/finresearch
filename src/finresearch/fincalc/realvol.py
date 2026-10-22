"""Realised-variance estimators, forecast losses and the HAR model (P3.7, #390). Pure functions on daily OHLC bars.

Per bar (prices of ONE contract; `prev_close` = that contract's close on the previous session):
  Parkinson (1980, J. Business 53, 61-65):          (ln H/L)^2 / (4 ln 2)
  Garman-Klass (1980, J. Business 53, 67-78):        (1/2)(ln H/L)^2 - (2 ln 2 - 1)(ln C/O)^2
  Rogers-Satchell (1991, Ann. Appl. Prob. 1, 504-512): ln(H/C) ln(H/O) + ln(L/C) ln(L/O)   (drift-independent)
  overnight o = ln(O / prev close); close-to-close r = ln(C / prev close); daily variance d = o^2 + RS (HAR's input)
Over a window of n >= 2 bars, per-session variance:
  close-to-close: the sample variance (n - 1) of r;  Yang-Zhang (2000, J. Business 73, 477-491, eq. 9):
  sigma^2 = V_o + k V_c + (1 - k) mean(RS), V_o and V_c the sample variances of o and ln(C/O),
  k = 0.34 / (1.34 + (n + 1) / (n - 1)).
Intraday (Andersen and Bollerslev 1998; Barndorff-Nielsen and Shephard 2004, J. Fin. Econometrics 2, 1-37):
  RV = sum r_i^2 (+ the overnight r^2), BV = (pi/2) sum |r_i||r_i-1|, jump share = max(RV - BV, 0) / RV.
Losses (Patton 2011, J. Econometrics 160, 246-256, robust to a noisy variance proxy):
  QLIKE = RV / f - ln(RV / f) - 1 (0 iff f = RV), MSE = (RV - f)^2.
HAR (Corsi 2009, J. Fin. Econometrics 7, 174-196) in logs, bias-corrected exp(x'b + s^2 / 2).

Unknown never becomes a number: a bar with a missing or non-positive price, or H below max(O, C), or L above
min(O, C) is invalid (`bar_problem` says why), and every estimate over a window holding one is None.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

LN2 = math.log(2)
YZ_ALPHA = 1.34  # Yang and Zhang's alpha (their p. 485: "1.34 ... in practice")


@dataclass(frozen=True)
class Bar:
    day: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    prev_close: float | None  # the SAME contract's close on the previous session; None when unknown


def bar_problem(b: Bar, need_prev: bool = False) -> str | None:
    """Why the bar cannot be used (None = valid); `need_prev` also requires the previous close."""
    if need_prev:
        return _prev_problem(b)
    for name in ("open", "high", "low", "close"):
        v = getattr(b, name)
        if v is None or not math.isfinite(v) or v <= 0:
            return f"{b.day}: {name} is {'missing' if v is None else v}"
    if b.high < max(b.open, b.close):  # type: ignore[operator, type-var]
        return f"{b.day}: high {b.high} below max(open, close)"
    if b.low > min(b.open, b.close):  # type: ignore[operator, type-var]
        return f"{b.day}: low {b.low} above min(open, close)"
    return None


def _prev_problem(b: Bar) -> str | None:
    if (p := bar_problem(b, need_prev=False)) is not None:
        return p
    if b.prev_close is None or not math.isfinite(b.prev_close) or b.prev_close <= 0:
        return f"{b.day}: previous close is {'missing' if b.prev_close is None else b.prev_close}"
    return None


def parkinson(b: Bar) -> float | None:
    return None if bar_problem(b) else math.log(b.high / b.low) ** 2 / (4 * LN2)  # type: ignore[operator]


def garman_klass(b: Bar) -> float | None:
    if bar_problem(b):
        return None
    hl, co = math.log(b.high / b.low), math.log(b.close / b.open)  # type: ignore[operator]
    return 0.5 * hl * hl - (2 * LN2 - 1) * co * co


def rogers_satchell(b: Bar) -> float | None:
    if bar_problem(b):
        return None
    h, lo, o, c = b.high, b.low, b.open, b.close
    return math.log(h / c) * math.log(h / o) + math.log(lo / c) * math.log(lo / o)  # type: ignore[operator]


def overnight(b: Bar) -> float | None:
    return None if _prev_problem(b) else math.log(b.open / b.prev_close)  # type: ignore[operator]


def close_to_close(b: Bar) -> float | None:
    return None if _prev_problem(b) else math.log(b.close / b.prev_close)  # type: ignore[operator]


def daily_variance(b: Bar) -> float | None:
    """o^2 + RS: one session's variance including the overnight gap (the HAR regressors' daily RV)."""
    o = overnight(b)
    return None if o is None else o * o + rogers_satchell(b)  # type: ignore[operator]


def _svar(xs: Sequence[float]) -> float:
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def yz_k(n: int) -> float:
    return (YZ_ALPHA - 1) / (YZ_ALPHA + (n + 1) / (n - 1))


def yang_zhang(bars: Sequence[Bar]) -> float | None:
    """Per-session Yang-Zhang variance over the bars; None below 2 bars or when any bar is invalid."""
    if len(bars) < 2 or any(_prev_problem(b) for b in bars):
        return None
    o = [math.log(b.open / b.prev_close) for b in bars]  # type: ignore[operator]
    c = [math.log(b.close / b.open) for b in bars]  # type: ignore[operator]
    k = yz_k(len(bars))
    return _svar(o) + k * _svar(c) + (1 - k) * sum(rogers_satchell(b) for b in bars) / len(bars)  # type: ignore[misc]


def close_to_close_variance(bars: Sequence[Bar]) -> float | None:
    r = [close_to_close(b) for b in bars]
    return None if len(r) < 2 or any(x is None for x in r) else _svar(r)  # type: ignore[arg-type]


def mean_of(f: Callable[[Bar], float | None], bars: Sequence[Bar]) -> float | None:
    """The window mean of a per-bar estimator (Parkinson, Garman-Klass, Rogers-Satchell); None if any bar is."""
    xs = [f(b) for b in bars]
    return None if not xs or any(x is None for x in xs) else sum(xs) / len(xs)  # type: ignore[arg-type, misc]


# ----------------------------------------------------------------------------------------------- intraday
def realized_variance(returns: Sequence[float | None], overnight: float | None = None) -> float | None:
    """Sum of squared intraday log returns (5-minute in the issue), plus the overnight return squared if given."""
    if not returns or any(r is None for r in returns):
        return None
    return sum(r * r for r in returns) + (overnight * overnight if overnight is not None else 0.0)  # type: ignore[operator]


def bipower_variation(returns: Sequence[float | None]) -> float | None:
    """(pi / 2) sum |r_i| |r_i-1|: jump-robust integrated variance (Barndorff-Nielsen and Shephard 2004)."""
    if len(returns) < 2 or any(r is None for r in returns):
        return None
    return math.pi / 2 * sum(abs(a) * abs(b) for a, b in zip(returns[1:], returns[:-1], strict=True))  # type: ignore[arg-type]


def jump_share(returns: Sequence[float | None]) -> float | None:
    """max(RV - BV, 0) / RV on the intraday returns (no overnight term): the share of variance from jumps."""
    a, b = realized_variance(returns), bipower_variation(returns)
    if a is None or b is None or a <= 0:
        return None
    return max(a - b, 0.0) / a


# ----------------------------------------------------------------------------------------------- losses
def qlike(realised: float | None, forecast: float | None) -> float | None:
    """RV / f - ln(RV / f) - 1; None when either is unknown or not positive (the loss is undefined there)."""
    if realised is None or forecast is None or realised <= 0 or forecast <= 0:
        return None
    x = realised / forecast
    return x - math.log(x) - 1


def mse(realised: float | None, forecast: float | None) -> float | None:
    return None if realised is None or forecast is None else (realised - forecast) ** 2


# ----------------------------------------------------------------------------------------------- vol cone
CONE_WINDOWS = (5, 10, 21, 63)
CONE_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)
SESSIONS_PER_YEAR = 252  # the cone's annualisation only; forecasts stay in total variance over the horizon


def vol_cone(bars: Sequence[Bar], windows: Sequence[int] = CONE_WINDOWS,
             quantiles: Sequence[float] = CONE_QUANTILES,
             estimator: Callable[[Sequence[Bar]], float | None] = yang_zhang) -> dict[int, dict]:  # fmt: skip
    """Per window length w: quantiles (numpy's linear rule) of sqrt(252 x per-session variance) over every rolling
    w-bar window, with the count of windows used and of windows left out as unknown (an invalid bar inside)."""
    out: dict[int, dict] = {}
    for w in windows:
        vals = [estimator(bars[i - w : i]) for i in range(w, len(bars) + 1)]
        known = [math.sqrt(SESSIONS_PER_YEAR * v) for v in vals if v is not None and v >= 0]
        qs = {q: float(np.quantile(known, q)) for q in quantiles} if known else {q: None for q in quantiles}
        out[w] = {"n": len(known), "unknown": len(vals) - len(known), "q": qs}
    return out


# ----------------------------------------------------------------------------------------------- OLS and HAR
# Corsi (2009): the daily, weekly (5) and monthly (22) means of the daily variance
HAR_WEEK, HAR_MONTH = 5, 22


@dataclass(frozen=True)
class Ols:
    coef: list[float]  # intercept first
    s2: float  # residual variance SSR / (n - p)
    n: int


def ols(x: Sequence[Sequence[float]], y: Sequence[float]) -> Ols | None:
    """y = b0 + x'b + e by least squares (numpy lstsq); None with too few rows (n <= p) or a rank-deficient design."""
    n = len(y)
    if n == 0:
        return None
    xx = np.column_stack([np.ones(n), np.asarray(x, float).reshape(n, -1)])
    p = xx.shape[1]
    if n <= p or np.linalg.matrix_rank(xx) < p:
        return None
    b, *_ = np.linalg.lstsq(xx, np.asarray(y, float), rcond=None)
    e = np.asarray(y, float) - xx @ b
    return Ols([float(v) for v in b], float(e @ e) / (n - p), n)


def log_forecast(fit: Ols, row: Sequence[float]) -> float:
    """exp(b0 + x'b + s^2 / 2): the mean of a lognormal whose log is the fitted value (the bias correction)."""
    return math.exp(fit.coef[0] + sum(b * x for b, x in zip(fit.coef[1:], row, strict=True)) + fit.s2 / 2)


def har_row(daily: Sequence[float | None], t: int) -> list[float] | None:
    """[ln d_t, ln mean(d over the 5 sessions to t), ln mean(d over the 22 sessions to t)]; None below 22 sessions or
    when any of them is unknown or not positive (ln undefined)."""
    if t < HAR_MONTH - 1 or t >= len(daily):
        return None
    m = daily[t - HAR_MONTH + 1 : t + 1]
    if any(v is None or v <= 0 for v in m):
        return None
    return [math.log(m[-1]), math.log(sum(m[-HAR_WEEK:]) / HAR_WEEK), math.log(sum(m) / HAR_MONTH)]  # type: ignore[arg-type, operator]


def mincer_zarnowitz(realised: Sequence[float], forecast: Sequence[float]) -> dict[str, float | int | None]:
    """RV = a + b f + u by OLS (reported only; an unbiased forecast has a = 0, b = 1) with its R^2."""
    fit = ols([[f] for f in forecast], realised)
    if fit is None:
        return {"n": len(realised), "a": None, "b": None, "r2": None}
    y = np.asarray(realised, float)
    tss = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - fit.s2 * (fit.n - 2) / tss if tss > 0 else None
    return {"n": fit.n, "a": fit.coef[0], "b": fit.coef[1], "r2": r2}


# ----------------------------------------------------------------------------------------------- GARCH horizon
def garch_horizon_variance(p, next_s2: float, h: int) -> float | None:  # p: taildist.GjrParams
    """sum_{k=1..h} E[s^2_t+k] = h s_bar + sum rho^(k-1) (s^2_t+1 - s_bar), rho = alpha + gamma / 2 + beta (symmetric
    shocks), s_bar = omega / (1 - rho); None when rho >= 1 (no long-run variance)."""
    rho = p.alpha + p.gamma / 2 + p.beta
    if rho >= 1 or h < 1:
        return None
    s_bar = p.omega / (1 - rho)
    return h * s_bar + (next_s2 - s_bar) * sum(rho**k for k in range(h))


GARCH_START = (0.05, 0.90)  # alpha, beta; omega = var x (1 - alpha - beta)


def garch11_fit(returns: Sequence[float], window: int, min_returns: int | None = None):  # -> taildist.GjrFit
    """Gaussian QMLE of a zero-mean GARCH(1,1) (Bollerslev 1986, J. Econometrics 31, 307-327) on the last `window`
    returns: taildist's GJR likelihood with gamma fixed at 0 (arch's backcast start), Nelder-Mead from GARCH_START
    with taildist's restarts and tolerances. Unknown (params None, a reason) when too short or not converged."""
    from finresearch.fincalc import taildist as td

    need = td.GJR_MIN_RETURNS if min_returns is None else min_returns
    r = [float(x) for x in list(returns)[-window:]]
    if len(r) < need:
        return td.GjrFit(None, None, len(r), f"{len(r)} returns, fewer than {need}")
    var, bc = float(np.var(r)), td.backcast(np.asarray(r))
    if var <= 0:
        return td.GjrFit(None, None, len(r), "zero variance")
    a, b = GARCH_START
    x0 = [1 - a - b, a, b]

    def nll(x: np.ndarray) -> float:
        return td._gjr_nll(r, bc, td.GjrParams(x[0] * var, x[1], 0.0, x[2]))

    res = None
    for _ in range(td.NM_RESTARTS + 1):
        res = td.nelder_mead(nll, x0, [td.NM_STEP * max(abs(v), 0.01) for v in x0])
        x0 = res.x
    assert res is not None
    if not res.converged or not math.isfinite(res.fun):
        return td.GjrFit(None, None, len(r), f"not converged after {res.nfev} evaluations")
    return td.GjrFit(td.GjrParams(res.x[0] * var, res.x[1], 0.0, res.x[2]), -res.fun, len(r))

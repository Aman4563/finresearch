"""Portfolio analytics arithmetic: pure functions, no database and no network (golden-tested in tests/test_portfolio_analytics.py).

Conventions
- A value series V(t) and an external-flow series F(t) share one list of trading days. F(t) is the *net money put in*
  on day t (a purchase is +, a sale's proceeds or a dividend paid out is -). Flows are assumed to happen at the day's
  close, the usual convention for daily time-weighted returns [K]:

      r(t) = (V(t) - F(t)) / V(t-1) - 1

  so buying or selling never looks like a gain or a loss. A day whose previous value is 0 (everything sold, then bought
  again) has no return and is skipped, never chained as a made-up number.
- Returns and statistics are plain floats (fractions). Rupee amounts are rounded only for display.
- Annualisation uses 252 trading days a year (the convention `fincalc.market` and `fincalc.funds` already use).

Formulas (research note "portfolio-insights" §1.7, §1.8, §1.4, §1.17):
- volatility σ_ann = stdev(r)·√252 (sample stdev), beta = cov(r_p, r_b) / var(r_b), tracking error = stdev(r_p - r_b)·√252;
- max drawdown = min_t (I_t / max_{s<=t} I_s - 1) on the TWR index, recovery = first day the index is back at that peak;
- historical VaR_95 = -q_0.05(r), CVaR_95 = -mean(r | r <= q_0.05); q by linear interpolation (numpy's default);
  the 21-day figures use overlapping 21-day returns (their effective sample is about n / 21);
- Sharpe = (mean(r)·252 - r_f) / σ_ann; Sortino divides by the downside deviation below r_f / 252
  (`fincalc.funds.sharpe_ratio` / `sortino_ratio`, applied to the TWR index); against each day's own risk-free
  return rf_t (#479): Sharpe = mean(r - rf)/stdev(r - rf)·√252, Sortino = mean(r - rf)·252 / (DD·√252) with
  DD = √(Σ min(0, r - rf)² / N) (`sharpe_ratio_excess` / `sortino_ratio_excess`);
- risk contribution RC_i = w_i·(Σw)_i / (wᵀΣw), fractions that add up to 1;
- marginal contribution MCR_i = (Σw)_i / σ_p (#399, the pre-trade line): new money in i raises σ_p iff MCR_i > σ_p;
- HHI = Σ w_i², effective number of holdings N_eff = 1 / HHI;
- direct index equivalent (Long–Nickels public-market equivalent): each net inflow buys F_t / B_t benchmark units and
  each outflow sells them; Kaplan–Schoar PME = (Σ out_t·B_T/B_t + V_T) / Σ in_t·B_T/B_t (> 1: the portfolio did better
  than the benchmark with the same money at the same times);
- regular → direct break-even years = one-off switch cost / yearly TER saving.

Benchmark-relative and rolling views (#349):
- information ratio = mean(r_p - r_b)·252 / tracking error (Grinold–Kahn, active return per unit of active risk);
- up / down capture = G(r_p | r_b > 0) / G(r_b | r_b > 0) and the same on r_b < 0 days, G the geometric mean per day
  (Morningstar's capture-ratio method, applied to daily rather than monthly returns; days with r_b = 0 are left out);
- rolling volatility and beta: the formulas above over each trailing window of `window` returns;
- drawdown episodes: every spell below a running peak (peak, trough, first day back at the peak), deepest first;
- VaR backtest, walk-forward: day t's 1-day historical VaR is estimated from the `window` returns before t only, and
  an exception is r_t < -VaR_t. Kupiec unconditional coverage LR_uc = -2 ln[(1-p)^(T-x) p^x] + 2 ln[(1-x/T)^(T-x)
  (x/T)^x] ~ χ²(1); Christoffersen independence LR_ind from the 0/1 transition counts ~ χ²(1); LR_cc = LR_uc + LR_ind
  ~ χ²(2) (Kupiec 1995; Christoffersen 1998). χ²(1) survival = erfc(√(x/2)), χ²(2) survival = e^(-x/2), both exact.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

TRADING_DAYS = 252
HORIZON_1M = 21  # trading days in a month


# --------------------------------------------------------------------------- time-weighted return
def twr_returns(values: Sequence[float], flows: Sequence[float]) -> list[float | None]:
    """Daily time-weighted returns; element 0 (and any day after a zero value) is None."""
    if len(values) != len(flows):
        raise ValueError("values and flows must have the same length")
    out: list[float | None] = [None] * len(values)
    for t in range(1, len(values)):
        prev = values[t - 1]
        if prev > 0:
            out[t] = (values[t] - flows[t]) / prev - 1
    return out


def chain(returns: Sequence[float | None], base: float = 1.0) -> list[float]:
    """Index from daily returns (a missing return leaves the index unchanged)."""
    idx, level = [], base
    for r in returns:
        if r is not None:
            level *= 1 + r
        idx.append(level)
    return idx


def defined_tail(returns: Sequence[float | None]) -> int:
    """First position of the longest tail in which every return (after the first element) is defined: the analysis
    window for risk statistics, so a full exit and re-entry does not splice two unrelated portfolios."""
    start = 0
    for i in range(1, len(returns)):
        if returns[i] is None:
            start = i
    return start


# --------------------------------------------------------------------------- risk
def annualised_vol(returns: Sequence[float], periods: int = TRADING_DAYS) -> float:
    if len(returns) < 2:
        raise ValueError("need at least 2 returns")
    return statistics.stdev(returns) * math.sqrt(periods)


def beta(rp: Sequence[float], rb: Sequence[float]) -> float:
    if len(rp) != len(rb) or len(rp) < 3:
        raise ValueError("need at least 3 paired returns")
    vb = statistics.variance(rb)
    if vb == 0:
        raise ValueError("benchmark returns have zero variance")
    return statistics.covariance(rp, rb) / vb


def tracking_error(rp: Sequence[float], rb: Sequence[float], periods: int = TRADING_DAYS) -> float:
    if len(rp) != len(rb) or len(rp) < 3:
        raise ValueError("need at least 3 paired returns")
    return statistics.stdev([a - b for a, b in zip(rp, rb, strict=True)]) * math.sqrt(periods)


def information_ratio(rp: Sequence[float], rb: Sequence[float], periods: int = TRADING_DAYS) -> float:
    te = tracking_error(rp, rb, periods)
    if te == 0:
        raise ValueError("tracking error is zero")
    return statistics.fmean([a - b for a, b in zip(rp, rb, strict=True)]) * periods / te


MIN_CAPTURE_DAYS = 20  # up days and down days each


def capture_ratios(rp: Sequence[float], rb: Sequence[float]) -> tuple[float, float]:
    """(up capture, down capture) as ratios (1.0 = moved as much as the benchmark on those days)."""
    if len(rp) != len(rb):
        raise ValueError("need paired returns")

    def g(xs: list[float]) -> float:
        return math.exp(statistics.fmean([math.log1p(x) for x in xs])) - 1

    out = []
    for side in (1, -1):
        pairs = [(a, b) for a, b in zip(rp, rb, strict=True) if b * side > 0]
        if len(pairs) < MIN_CAPTURE_DAYS:
            raise ValueError(
                f"needs {MIN_CAPTURE_DAYS} {'up' if side > 0 else 'down'} days; have {len(pairs)}"
            )
        out.append(g([a for a, _ in pairs]) / g([b for _, b in pairs]))
    return out[0], out[1]


@dataclass(frozen=True)
class Rolling:
    end: int  # position of the window's last return
    volatility: float
    beta: float | None  # None without a benchmark (or one with no variance in the window)


def rolling_stats(rp: Sequence[float], rb: Sequence[float] | None, window: int) -> list[Rolling]:
    """Volatility and beta over each trailing `window` of returns; nothing before the first full window."""
    out = []
    for t in range(window - 1, len(rp)):
        p = rp[t - window + 1 : t + 1]
        b = None
        if rb is not None:
            try:
                b = beta(p, rb[t - window + 1 : t + 1])
            except ValueError:
                b = None
        out.append(Rolling(t, annualised_vol(p), b))
    return out


@dataclass(frozen=True)
class Episode:
    peak: int
    trough: int
    recovered: int | None  # None: still below that peak at the last point
    depth: float  # trough / peak - 1, < 0


def drawdown_episodes(index: Sequence[float], top: int = 5) -> list[Episode]:
    """Every spell below a running peak, deepest first (ties: the earlier first), at most `top`."""
    eps, peak, trough, start = [], 0, None, None
    for t in range(1, len(index)):
        if index[t] >= index[peak]:
            if start is not None:
                eps.append(Episode(peak, trough, t, index[trough] / index[peak] - 1))
                start = None
            peak = t
        else:
            if start is None:
                start, trough = t, t
            elif index[t] < index[trough]:
                trough = t
    if start is not None:
        eps.append(Episode(peak, trough, None, index[trough] / index[peak] - 1))
    return sorted(eps, key=lambda e: (e.depth, e.peak))[:top]


def var_exceptions(returns: Sequence[float], window: int = 250, level: float = 0.95) -> list[bool]:
    """Walk-forward exceptions: day t (t >= window) against the historical VaR of returns[t-window:t]."""
    return [
        returns[t] < -historical_var(returns[t - window : t], level)[0] for t in range(window, len(returns))
    ]


def _xlogy(x: float, y: float) -> float:
    return 0.0 if x == 0 else x * math.log(y)


def kupiec(n: int, x: int, p: float) -> tuple[float, float]:
    """(LR_uc, p-value) for x exceptions in n days at exception rate p."""
    if n <= 0 or not 0 <= x <= n:
        raise ValueError("need 0 <= x <= n and n > 0")
    lr = -2 * (_xlogy(n - x, 1 - p) + _xlogy(x, p)) + 2 * (_xlogy(n - x, 1 - x / n) + _xlogy(x, x / n))
    lr = max(lr, 0.0)
    return lr, chi2_sf(lr, 1)


def christoffersen_ind(hits: Sequence[bool | int]) -> float:
    """LR_ind: do exceptions cluster (does yesterday's exception change today's chance of one)?"""
    n = [[0, 0], [0, 0]]
    for a, b in itertools.pairwise(hits):
        n[int(a)][int(b)] += 1
    n00, n01, n10, n11 = n[0][0], n[0][1], n[1][0], n[1][1]
    tot = n00 + n01 + n10 + n11
    if tot == 0:
        raise ValueError("need at least 2 days")
    pi = (n01 + n11) / tot
    p01 = n01 / (n00 + n01) if n00 + n01 else 0.0
    p11 = n11 / (n10 + n11) if n10 + n11 else 0.0
    l0 = _xlogy(n00 + n10, 1 - pi) + _xlogy(n01 + n11, pi)
    l1 = _xlogy(n00, 1 - p01) + _xlogy(n01, p01) + _xlogy(n10, 1 - p11) + _xlogy(n11, p11)
    return max(-2 * (l0 - l1), 0.0)


def chi2_sf(x: float, df: int) -> float:
    """χ² survival function for 1 or 2 degrees of freedom (closed forms)."""
    if df == 1:
        return math.erfc(math.sqrt(max(x, 0.0) / 2))
    if df == 2:
        return math.exp(-max(x, 0.0) / 2)
    raise ValueError("df must be 1 or 2")


@dataclass(frozen=True)
class DrawdownInfo:
    depth: float  # fraction, <= 0
    peak: int  # positions in the series
    trough: int
    recovered: int | None  # first position back at (or above) the peak, None if not yet
    current: float  # where the last point sits below the running peak (<= 0)


def drawdown_recovery(index: Sequence[float]) -> DrawdownInfo:
    """Max drawdown (via `fincalc.market.max_drawdown`) plus when, if ever, the index regained that peak."""
    from finresearch.fincalc.market import max_drawdown

    if len(index) < 2:
        raise ValueError("need at least 2 points")
    dd = max_drawdown(index)
    peak_level = index[dd.peak_index]
    rec = next((j for j in range(dd.trough_index + 1, len(index)) if index[j] >= peak_level), None)
    if float(dd.max_drawdown) == 0:
        rec = None
    return DrawdownInfo(
        float(dd.max_drawdown), dd.peak_index, dd.trough_index, rec, index[-1] / max(index) - 1
    )


def horizon_returns(index: Sequence[float], h: int = HORIZON_1M) -> list[float]:
    """Overlapping h-day returns I[t+h] / I[t] - 1."""
    return [index[t + h] / index[t] - 1 for t in range(len(index) - h) if index[t] > 0]


def historical_var(returns: Sequence[float], level: float = 0.95) -> tuple[float, float]:
    """(VaR, CVaR) as positive loss fractions at `level` from the empirical distribution."""
    if len(returns) < 20:
        raise ValueError("need at least 20 returns")
    arr = np.asarray(returns, dtype=float)
    q = float(np.quantile(arr, 1 - level))  # linear interpolation
    tail = arr[arr <= q]
    return -q, -float(tail.mean())


def sharpe_sortino(
    index: Sequence[float], days: Sequence[date], rf_annual: float
) -> tuple[float, float | None]:
    """(Sharpe, Sortino) of the TWR index through `fincalc.funds` (the same formulas as the fund pages)."""
    from finresearch.fincalc.funds import sharpe_ratio, sortino_ratio

    navs = list(zip(days, index, strict=True))
    s = float(sharpe_ratio(navs, rf_annual))
    try:
        so: float | None = float(sortino_ratio(navs, rf_annual))
    except ValueError:  # no downside periods at all
        so = None
    return s, so


def sharpe_sortino_excess(
    index: Sequence[float], days: Sequence[date], rf: Sequence[float]
) -> tuple[float, float | None]:
    """(Sharpe, Sortino) of the TWR index against each day's own risk-free return `rf[i]` (over days[i]..days[i+1],
    #479): fincalc.funds.sharpe_ratio_excess / sortino_ratio_excess, 252 periods a year."""
    from finresearch.fincalc.funds import sharpe_ratio_excess, sortino_ratio_excess

    navs = list(zip(days, index, strict=True))
    s = float(sharpe_ratio_excess(navs, rf))
    try:
        so: float | None = float(sortino_ratio_excess(navs, rf))
    except ValueError:  # no day below its risk-free return
        so = None
    return s, so


def risk_contributions(
    weights: Sequence[float], returns: Sequence[Sequence[float]]
) -> tuple[list[float], float]:
    """(each position's share of portfolio variance, annualised portfolio volatility) for weights `w` and a
    T x N matrix of aligned daily returns (sample covariance)."""
    w = np.asarray(weights, dtype=float)
    r = np.asarray(returns, dtype=float)
    if r.ndim != 2 or r.shape[1] != len(w) or r.shape[0] < 3:
        raise ValueError("returns must be T x N with T >= 3")
    cov = np.atleast_2d(np.cov(r, rowvar=False, ddof=1))
    var_p = float(w @ cov @ w)
    if var_p <= 0:
        raise ValueError("portfolio variance is zero")
    rc = w * (cov @ w) / var_p
    return [float(x) for x in rc], math.sqrt(var_p * TRADING_DAYS)


@dataclass(frozen=True)
class TradeRisk:
    sigma_before: float  # annualised portfolio volatility, today's mix
    sigma_after: float  # the same after the trade (new money in for a buy, proceeds out for a sale)
    mcr: float  # holding i's marginal contribution (Σw)_i / σ_p, annualised: new money in i raises σ_p iff mcr > σ_p
    correlation: (
        float | None
    )  # of holding i's daily returns with holding j's (None: no j, or a constant series)


def trade_risk(values_before: Sequence[float], values_after: Sequence[float], returns: Sequence[Sequence[float]],
               i: int, j: int | None) -> TradeRisk:  # fmt: skip
    """The pre-trade marginal-risk line (#399) under the sample covariance of a T x N matrix of aligned daily returns.
    σ_p is homogeneous of degree 1 in w, so Σ w_k MCR_k = σ_p and adding ε of new money to i moves σ_p by about
    ε·(MCR_i - σ_p)."""
    r = np.asarray(returns, dtype=float)
    wb, wa = np.asarray(values_before, dtype=float), np.asarray(values_after, dtype=float)
    if r.ndim != 2 or r.shape[1] != len(wb) or len(wa) != len(wb) or r.shape[0] < 3:
        raise ValueError("returns must be T x N with T >= 3")
    if wb.sum() <= 0 or wa.sum() <= 0:
        raise ValueError("values must add up to more than 0")
    cov = np.atleast_2d(np.cov(r, rowvar=False, ddof=1)) * TRADING_DAYS
    wb, wa = wb / wb.sum(), wa / wa.sum()
    sb = math.sqrt(float(wb @ cov @ wb))
    if sb <= 0:
        raise ValueError("portfolio variance is zero")
    corr = None
    if j is not None and cov[i, i] > 0 and cov[j, j] > 0:
        corr = float(cov[i, j] / math.sqrt(cov[i, i] * cov[j, j]))
    return TradeRisk(sb, math.sqrt(float(wa @ cov @ wa)), float((cov @ wb)[i] / sb), corr)


# --------------------------------------------------------------------------- concentration
def hhi(weights: Sequence[float]) -> float:
    total = sum(weights)
    if total <= 0:
        raise ValueError("weights must add up to more than 0")
    return sum((x / total) ** 2 for x in weights)


def n_effective(weights: Sequence[float]) -> float:
    return 1 / hhi(weights)


def top_share(weights: Sequence[float], n: int = 5) -> float:
    total = sum(weights)
    return sum(sorted(weights, reverse=True)[:n]) / total if total > 0 else 0.0


# --------------------------------------------------------------------------- benchmark with the same cash flows
@dataclass(frozen=True)
class Equivalent:
    units: float  # benchmark units held at the end
    value: float  # at the last benchmark level
    clamped: bool  # an outflow was larger than the benchmark holding (units floored at 0)
    pme: float | None  # Kaplan–Schoar PME of the actual portfolio against the benchmark


def direct_index_equivalent(flows: Sequence[tuple[date, float]], level: dict[date, float], end: date,
                            actual_value: float) -> Equivalent:  # fmt: skip
    """Invest each net flow (+ in / - out) in the benchmark at that day's level; value it at `end`."""
    if end not in level:
        raise ValueError("no benchmark level on the end day")
    units, clamped = 0.0, False
    fv_in = fv_out = 0.0
    b_end = level[end]
    for d, f in sorted(flows, key=lambda x: x[0]):
        b = level.get(d)
        if b is None or b <= 0:
            raise ValueError(f"no benchmark level on {d}")
        units += f / b
        if units < -1e-9:
            units, clamped = 0.0, True
        if f > 0:
            fv_in += f * b_end / b
        elif f < 0:
            fv_out += -f * b_end / b
    pme = (fv_out + actual_value) / fv_in if fv_in > 0 else None
    return Equivalent(units, units * b_end, clamped, pme)


def investor_xirr(flows: Sequence[tuple[date, float]], end: date, end_value: float) -> float:
    """XIRR from the investor's side: net inflows are money out of the pocket (negative), the end value comes back."""
    from finresearch.fincalc.funds import xirr

    cf = [(d, -f) for d, f in flows if f]
    if end_value:
        cf.append((end, end_value))
    return float(xirr(cf))


def total_return_index(days: Sequence[date], closes: Sequence[float], dividends: dict[date, float],
                       unit_factor: dict[date, float] | None = None) -> list[float]:  # fmt: skip
    """Total-return index from raw closes: TR_t = TR_{t-1}·(P_t·f_t + D_t) / P_{t-1}, where D_t is the cash dividend
    per (old) unit with ex-date t, reinvested at the close, and f_t the unit multiplier of a split/bonus on t
    (10 -> 1 split: f = 10). Dividends and splits on non-trading days are moved to the next trading day."""
    unit_factor = unit_factor or {}
    out, level = [], 1.0
    pend_d, pend_f = 0.0, 1.0
    di = sorted(dividends.items())
    fi = sorted(unit_factor.items())
    i = j = 0
    for t, d in enumerate(days):
        while i < len(di) and di[i][0] <= d:
            if t > 0 and di[i][0] > days[t - 1]:
                pend_d += di[i][1]
            i += 1
        while j < len(fi) and fi[j][0] <= d:
            if t > 0 and fi[j][0] > days[t - 1]:
                pend_f *= fi[j][1]
            j += 1
        if t > 0:
            level *= (closes[t] * pend_f + pend_d) / closes[t - 1]
        pend_d, pend_f = 0.0, 1.0
        out.append(level)
    return out


# --------------------------------------------------------------------------- costs
def break_even_years(switch_cost: float, yearly_saving: float) -> float | None:
    if yearly_saving <= 0:
        return None
    return max(switch_cost, 0.0) / yearly_saving

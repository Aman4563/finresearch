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
  (`fincalc.funds.sharpe_ratio` / `sortino_ratio`, applied to the TWR index);
- risk contribution RC_i = w_i·(Σw)_i / (wᵀΣw), fractions that add up to 1;
- HHI = Σ w_i², effective number of holdings N_eff = 1 / HHI;
- direct index equivalent (Long–Nickels public-market equivalent): each net inflow buys F_t / B_t benchmark units and
  each outflow sells them; Kaplan–Schoar PME = (Σ out_t·B_T/B_t + V_T) / Σ in_t·B_T/B_t (> 1: the portfolio did better
  than the benchmark with the same money at the same times);
- regular → direct break-even years = one-off switch cost / yearly TER saving.
"""

from __future__ import annotations

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

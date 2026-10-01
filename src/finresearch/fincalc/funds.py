"""Mutual-fund arithmetic: point-to-point and rolling returns, XIRR, SIP outcomes, risk-adjusted returns and costs.

NAV series are sequences of (date, NAV) in chronological order. Annualised returns use actual days / 365.
Risk-free rates are inputs (state the source and date); nothing here assumes one.
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from finresearch.fincalc.dates import add_years
from finresearch.fincalc.numbers import Num, require_price, to_decimal

Series = Sequence[tuple[date, Num]]


def _series(navs: Series, minimum: int = 2) -> list[tuple[date, Decimal]]:
    s = sorted(((d, require_price(v, "nav")) for d, v in navs), key=lambda x: x[0])
    if len(s) < minimum:
        raise ValueError(f"need at least {minimum} NAVs, got {len(s)}")
    return s


def annualised_return(start_nav: Num, end_nav: Num, start: date, end: date) -> Decimal:
    """``(end / start) ** (365 / days) - 1``; periods under a year are annualised too (label them)."""
    days = (end - start).days
    if days <= 0:
        raise ValueError("end must be after start")
    ratio = require_price(end_nav, "end_nav") / require_price(start_nav, "start_nav")
    with localcontext() as ctx:
        ctx.prec = 28
        return Decimal(float(ratio) ** (365 / days)).quantize(Decimal("1e-10")) - 1


def nav_on_or_before(navs: Series, day: date) -> tuple[date, Decimal] | None:
    s = _series(navs, 1)
    best = None
    for d, v in s:
        if d <= day:
            best = (d, v)
    return best


def trailing_return(navs: Series, years: int) -> Decimal | None:
    """Annualised return over the last `years` years (None if the history is shorter)."""
    s = _series(navs)
    end_d, end_v = s[-1]
    start = nav_on_or_before(s, add_years(end_d, -years))
    if start is None or (end_d - start[0]).days < 365 * years - 7:
        return None
    return annualised_return(start[1], end_v, start[0], end_d)


@dataclass(frozen=True)
class RollingStats:
    window_years: int
    count: int
    minimum: Decimal
    median: Decimal
    maximum: Decimal
    share_positive: Decimal


def rolling_return_series(navs: Series, years: int, step_days: int = 7) -> list[tuple[date, Decimal]]:
    """(window start, annualised return) for every `years`-long window, sampled every `step_days`; empty if the
    history is shorter than one window."""
    s = _series(navs)
    out, i = [], 0
    while i < len(s):
        d0, v0 = s[i]
        target = add_years(d0, years)
        end = next(((d, v) for d, v in s[i:] if d >= target), None)
        if end is None:
            break
        out.append((d0, annualised_return(v0, end[1], d0, end[0])))
        nxt = next((j for j in range(i + 1, len(s)) if (s[j][0] - d0).days >= step_days), None)
        if nxt is None:
            break
        i = nxt
    return out


def rolling_returns(navs: Series, years: int, step_days: int = 7) -> RollingStats | None:
    """Annualised returns over every `years`-long window (sampled every `step_days`); None if history is short."""
    out = [r for _, r in rolling_return_series(navs, years, step_days)]
    if not out:
        return None
    srt = sorted(out)
    mid = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
    return RollingStats(
        years, len(out), srt[0], mid, srt[-1], Decimal(sum(1 for x in out if x > 0)) / len(out)
    )


def xirr(cashflows: Sequence[tuple[date, Num]], guess: float = 0.1) -> Decimal:
    """Annualised internal rate of return of dated cash flows (investments negative, redemptions positive)."""
    cf = sorted(((d, float(to_decimal(a))) for d, a in cashflows), key=lambda x: x[0])
    if not (any(a < 0 for _, a in cf) and any(a > 0 for _, a in cf)):
        raise ValueError("xirr needs at least one negative and one positive cash flow")
    t0 = cf[0][0]

    def npv(r: float) -> float:
        return sum(a / (1 + r) ** ((d - t0).days / 365) for d, a in cf)

    lo, hi = -0.9999, 10.0
    if npv(lo) * npv(hi) > 0:
        raise ValueError("xirr has no solution in (-99.99%, 1000%)")
    for _ in range(200):  # bisection: slow but always converges on a bracketed root
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return Decimal(str(round((lo + hi) / 2, 8)))


@dataclass(frozen=True)
class SipOutcome:
    invested: Decimal
    units: Decimal
    value: Decimal
    xirr: Decimal
    instalments: int


def sip_outcome(navs: Series, amount: Num, start: date, end: date, day_of_month: int = 1) -> SipOutcome:
    """Monthly SIP of `amount` on `day_of_month` (the next NAV date if that day has none), valued at the last NAV
    on or before `end`. A day the month lacks (the 31st in April, the 29th-31st in February) falls on the month's
    last day."""
    s = _series(navs)
    amt = require_price(amount, "amount")
    units, invested, flows, n = Decimal(0), Decimal(0), [], 0
    y, m = start.year, start.month
    while True:
        due = date(y, m, min(day_of_month, calendar.monthrange(y, m)[1]))
        if due > end:
            break
        if due >= start:
            fill = next(((d, v) for d, v in s if d >= due), None)
            if fill is None or fill[0] > end:
                break
            units += amt / fill[1]
            invested += amt
            flows.append((fill[0], -amt))
            n += 1
        m += 1
        if m > 12:
            y, m = y + 1, 1
    last = nav_on_or_before(s, end)
    if not n or last is None:
        raise ValueError("no SIP instalment falls inside the NAV history")
    value = units * last[1]
    flows.append((last[0], value))
    return SipOutcome(invested, units, value, xirr(flows), n)


def _period_returns(navs: Series) -> list[Decimal]:
    s = _series(navs, 3)
    return [s[i][1] / s[i - 1][1] - 1 for i in range(1, len(s))]


def sharpe_ratio(navs: Series, risk_free_annual: Num, periods_per_year: int = 252) -> Decimal:
    """(annualised mean period return - risk-free) / annualised volatility, from daily NAVs."""
    r = _period_returns(navs)
    with localcontext() as ctx:
        ctx.prec = 28
        mean = sum(r, Decimal(0)) / len(r)
        sd = (sum(((x - mean) ** 2 for x in r), Decimal(0)) / (len(r) - 1)).sqrt()
        if sd == 0:
            raise ValueError("volatility is zero")
        return (mean * periods_per_year - to_decimal(risk_free_annual)) / (
            sd * Decimal(periods_per_year).sqrt()
        )


def sortino_ratio(navs: Series, risk_free_annual: Num, periods_per_year: int = 252) -> Decimal:
    """Like Sharpe but divides by downside deviation (returns below the per-period risk-free rate)."""
    r = _period_returns(navs)
    rf = to_decimal(risk_free_annual) / periods_per_year
    with localcontext() as ctx:
        ctx.prec = 28
        downside = [min(Decimal(0), x - rf) for x in r]
        dd = (sum((x * x for x in downside), Decimal(0)) / len(r)).sqrt()
        if dd == 0:
            raise ValueError("no downside periods")
        mean = sum(r, Decimal(0)) / len(r)
        return (mean - rf) * periods_per_year / (dd * Decimal(periods_per_year).sqrt())


def expense_drag(amount: Num, years: Num, gross_return: Num, ter_high: Num, ter_low: Num) -> Decimal:
    """Rupee difference after `years` between paying `ter_low` and `ter_high` on the same gross return (the cost of
    a regular plan's commission, for example)."""
    a, y, g = require_price(amount, "amount"), to_decimal(years), to_decimal(gross_return)
    hi, lo = to_decimal(ter_high), to_decimal(ter_low)
    with localcontext() as ctx:
        ctx.prec = 28
        return a * ((1 + g - lo) ** int(y) - (1 + g - hi) ** int(y)) if y == int(y) else a * (
            Decimal(float(1 + g - lo) ** float(y)) - Decimal(float(1 + g - hi) ** float(y)))  # fmt: skip


# --------------------------------------------------------------------------- consistency against category peers
# Probabilities and ranks below are plain floats (0..1): they are statistics about returns, not money amounts.


def wilson_interval(successes: float, n: float, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion (95 % by default). Centre (p + z²/2n) / (1 + z²/n), half-width
    z·√(p(1-p)/n + z²/4n²) / (1 + z²/n). `n` may be fractional (an effective sample size). Example: 7 of 10 gives
    about 0.397-0.892 (docs/dev/RESEARCH_ROADMAP.md §C.10)."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    p, z2 = successes / n, z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * (p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5 / (1 + z2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def effective_windows(count: int, step_days: float, window_days: float) -> float:
    """Roughly how many independent observations `count` overlapping windows hold: 1 + (count - 1) x step/window.
    Quarterly 3-year windows overlap by 11/12, so 13 of them are worth about 2 independent 3-year periods."""
    if count <= 0:
        return 0.0
    return 1 + (count - 1) * min(1.0, step_days / window_days)


def percentile_rank(values: Sequence[float], x: float) -> float:
    """Share of `values` below `x`, counting ties as half (0 = worst, 1 = best when higher is better)."""
    if not values:
        raise ValueError("no values")
    below = sum(1 for v in values if v < x)
    ties = sum(1 for v in values if v == x)
    return (below + 0.5 * ties) / len(values)


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("no values")
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def downside_capture(fund: Sequence[float], benchmark: Sequence[float]) -> float | None:
    """Sum of the fund's period returns over the sum of the benchmark's, in the periods the benchmark fell. Below 1
    means the fund fell less than the benchmark in its down periods. None when the benchmark never fell."""
    if len(fund) != len(benchmark):
        raise ValueError("series must have the same length")
    pairs = [(f, b) for f, b in zip(fund, benchmark, strict=True) if b < 0]
    if not pairs:
        return None
    return sum(f for f, _ in pairs) / sum(b for _, b in pairs)


def r_squared(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Square of the Pearson correlation between two return series: the share of y's variation that moves with x.
    None if either series is constant."""
    if len(x) != len(y) or len(x) < 3:
        raise ValueError("need two series of the same length, at least 3 points")
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sxy * sxy / (sxx * syy)

"""Market arithmetic for listed stocks: returns, volatility, drawdowns, moving averages, dividends and ranges.

Price series are sequences of closes in chronological order (oldest first). Returns are fractions. Volatility
uses sample standard deviation of simple daily returns annualised by sqrt(periods_per_year) (252 trading days).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from itertools import pairwise

from finresearch.fincalc.numbers import Num, opt_decimal, require_price, to_decimal

TRADING_DAYS = 252


def _closes(closes: Sequence[Num], minimum: int = 2) -> list[Decimal]:
    out = [require_price(c, "close") for c in closes]
    if len(out) < minimum:
        raise ValueError(f"need at least {minimum} closes, got {len(out)}")
    return out


def price_return(start: Num, end: Num) -> Decimal:
    """Simple return ``end / start - 1``."""
    return require_price(end, "end") / require_price(start, "start") - 1


def total_return(start: Num, end: Num, dividends: Num = 0) -> Decimal:
    """Holding-period return including cash dividends per share received: ``(end + dividends) / start - 1``."""
    return (require_price(end, "end") + to_decimal(dividends)) / require_price(start, "start") - 1


def daily_returns(closes: Sequence[Num]) -> list[Decimal]:
    c = _closes(closes)
    return [c[i] / c[i - 1] - 1 for i in range(1, len(c))]


def annualised_volatility(closes: Sequence[Num], periods_per_year: int = TRADING_DAYS) -> Decimal:
    """Sample standard deviation of simple period returns x sqrt(periods_per_year)."""
    r = daily_returns(_closes(closes, 3))
    with localcontext() as ctx:
        ctx.prec = 28
        mean = sum(r, Decimal(0)) / len(r)
        var = sum(((x - mean) ** 2 for x in r), Decimal(0)) / (len(r) - 1)
        return var.sqrt() * Decimal(periods_per_year).sqrt()


@dataclass(frozen=True)
class Drawdown:
    max_drawdown: Decimal  # fraction, negative or zero
    peak_index: int
    trough_index: int


def max_drawdown(closes: Sequence[Num]) -> Drawdown:
    """Largest peak-to-trough fall ``trough / peak - 1`` over the series (0 if it never falls)."""
    c = _closes(closes)
    peak_i, best = 0, Drawdown(Decimal(0), 0, 0)
    for i, x in enumerate(c):
        if x > c[peak_i]:
            peak_i = i
        dd = x / c[peak_i] - 1
        if dd < best.max_drawdown:
            best = Drawdown(dd, peak_i, i)
    return best


# --------------------------------------------------------------------------- beta for a cost of equity (#504)
# The slope of the stock's returns on the index's (the market model: Sharpe, "A Simplified Model for Portfolio
# Analysis", Management Science 9(2), 1963; the beta of the CAPM, Sharpe 1964), by ordinary least squares with an
# intercept: beta = Sxy / Sxx, SE = sqrt((SSE / (n - 2)) / Sxx). The registered F&O beta (fincalc.beta, #394) uses
# the same formulas with EWMA weights on futures returns; with equal weights they are these (a test checks it),
# and that module and its spec are not used or changed here.
#
# BETA_MIN_RETURNS = 60 [U]: five years of monthly returns, the conventional sample for a valuation beta (the window
# Fama & MacBeth estimate betas on in "Risk, Return, and Equilibrium", JPE 81(3), 1973 [U: window as recalled], and
# the "5-year monthly" beta the outside dcf-model skill names). It is a floor on the sample, not a promise of
# precision: the standard error is returned and belongs beside the beta. A constant, so a caller cannot lower it.
# BETA_MAX_AGE_DAYS = 7 [U]: the last close may be at most a week before `as_of` (a long weekend with exchange
# holidays is 4 to 5 calendar days; the registered F&O beta uses the same 7).
BETA_MIN_RETURNS = 60
BETA_MAX_AGE_DAYS = 7


@dataclass(frozen=True)
class Beta:
    """OLS beta with its standard error, sample and window, and what was and was not checked about the series."""

    beta: Decimal
    alpha: Decimal  # the intercept, per period
    se: Decimal  # standard error of beta
    r_squared: Decimal
    n: int  # paired returns
    window_start: date | None  # the first and last close's dates, when dates were given
    window_end: date | None
    as_of: date | None
    alignment: str  # "dates match", or "not checked: ..."
    staleness: str  # "the series ends N days before as_of ...", or "not checked: ..."
    largest_move: (
        Decimal  # the stock's largest one-period return by size (signed): look here for a missed split
    )
    largest_move_date: date | None
    min_returns: int
    max_age_days: int
    method: str


def _ols(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """(beta, alpha, SE of beta, R^2) of y = alpha + beta x by ordinary least squares; needs n >= 3 and both
    series varying (the caller checks)."""
    n = len(xs)
    with localcontext() as ctx:
        ctx.prec = 28
        xb, yb = sum(xs, Decimal(0)) / n, sum(ys, Decimal(0)) / n
        sxx = sum(((x - xb) ** 2 for x in xs), Decimal(0))
        syy = sum(((y - yb) ** 2 for y in ys), Decimal(0))
        b = sum(((x - xb) * (y - yb) for x, y in zip(xs, ys, strict=True)), Decimal(0)) / sxx
        a = yb - b * xb
        sse = sum(((y - a - b * x) ** 2 for x, y in zip(xs, ys, strict=True)), Decimal(0))
        return b, a, (sse / (n - 2) / sxx).sqrt(), 1 - sse / syy


def _dates(xs: object, name: str, n: int) -> list[date]:
    if not isinstance(xs, list | tuple) or len(xs) != n:
        raise ValueError(f"{name} must hold one date for each close ({n})")
    out = [date.fromisoformat(d) if isinstance(d, str) else d for d in xs]
    if any(b <= a for a, b in pairwise(out)):
        raise ValueError(f"{name} must be in date order, oldest first, without repeats")
    return out


def beta(stock_closes: Sequence[Num], index_closes: Sequence[Num], stock_dates: Sequence[date] | None = None,
         index_dates: Sequence[date] | None = None, as_of: date | None = None) -> Beta:  # fmt: skip
    """OLS beta of a stock on an index from two aligned series of ADJUSTED closes, with its standard error; cite `beta`.

    Both series oldest first, one close per period on the same days (daily, weekly or monthly: the caller's
    choice), already adjusted for splits and bonuses: an unadjusted split is a one-day "return" of -50 % that
    wrecks the estimate, so check `largest_move`. Returns are simple (P_t / P_(t-1) - 1). With `stock_dates` and
    `index_dates` ("YYYY-MM-DD") the two series must have the same dates, and with `as_of` (the valuation date)
    the last close must be at most 7 days before it; without them `alignment` / `staleness` say "not checked".
    ValueError, with the reason, for a short series (fewer than 60 returns), a misaligned or stale one, a
    non-positive close, or a series that does not vary (no beta: unknown, not 0). Raw OLS: no Blume or Vasicek
    shrinkage towards 1 is applied.
    """
    stock, index = _closes(stock_closes), _closes(index_closes)
    if len(stock) != len(index):
        raise ValueError(f"misaligned: {len(stock)} stock closes and {len(index)} index closes")
    if (stock_dates is None) != (index_dates is None):
        raise ValueError("give both stock_dates and index_dates, or neither")
    days = None
    alignment = staleness = "not checked: no dates given"
    if stock_dates is not None:
        days, other = (
            _dates(stock_dates, "stock_dates", len(stock)),
            _dates(index_dates, "index_dates", len(index)),
        )
        i = next((i for i, (a, b) in enumerate(zip(days, other, strict=True)) if a != b), None)
        if i is not None:
            raise ValueError(
                f"misaligned: the dates differ from position {i} (stock {days[i]}, index {other[i]})"
            )
        alignment, staleness = "dates match", "not checked: no as_of given"
    when = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    if when is not None:
        if days is None:
            raise ValueError("as_of needs the dates of the closes: give stock_dates and index_dates")
        age = (when - days[-1]).days
        if age < 0:
            raise ValueError(f"as_of {when} is before the last close ({days[-1]})")
        if age > BETA_MAX_AGE_DAYS:
            raise ValueError(f"stale: the series ends on {days[-1]}, {age} days before as_of {when} (at most "
                             f"{BETA_MAX_AGE_DAYS})")  # fmt: skip
        staleness = f"the series ends {age} days before as_of (at most {BETA_MAX_AGE_DAYS})"
    ys, xs = daily_returns(stock), daily_returns(index)
    if len(ys) < BETA_MIN_RETURNS:
        raise ValueError(f"short: {len(ys)} returns, at least {BETA_MIN_RETURNS} are needed")
    if len(set(xs)) == 1:
        raise ValueError("the index's returns do not vary: no beta can be estimated")
    if len(set(ys)) == 1:
        raise ValueError("the stock's returns do not vary: a price that never moves has no beta")
    b, a, se, r2 = _ols(xs, ys)
    k = max(range(len(ys)), key=lambda j: (abs(ys[j]), -j))  # the largest move; the earliest on a tie
    return Beta(b, a, se, r2, len(ys), days[0] if days else None, days[-1] if days else None, when, alignment,
                staleness, ys[k], days[k + 1] if days else None, BETA_MIN_RETURNS, BETA_MAX_AGE_DAYS,
                "ordinary least squares of simple returns, stock on index, with an intercept")  # fmt: skip


def moving_average(closes: Sequence[Num], window: int) -> Decimal:
    """Simple moving average of the last ``window`` closes."""
    if window < 1:
        raise ValueError("window must be >= 1")
    c = _closes(closes, window)
    return sum(c[-window:], Decimal(0)) / window


def dividend_yield(dividends_per_share: Num | None, price: Num) -> Decimal | None:
    """Trailing dividend yield ``DPS / price``."""
    d = opt_decimal(dividends_per_share)
    return None if d is None else d / require_price(price)


def range_position(price: Num, low: Num, high: Num) -> Decimal:
    """Where the price sits in its range: 0 at the low, 1 at the high (e.g. the 52-week range)."""
    p, lo, hi = require_price(price), require_price(low, "low"), require_price(high, "high")
    if hi <= lo:
        raise ValueError("high must be above low")
    return (p - lo) / (hi - lo)


_DIVIDEND = re.compile(
    r"dividend[^0-9]*?(?:rs\.?|re\.?|₹|inr)\s*(\d+(?:\.\d+)?)\s*(?:/-)?\s*per\s*share", re.I
)


def dividend_per_share(subject: str) -> Decimal | None:
    """Cash dividend per share from an NSE corporate-action subject, e.g. 'Dividend - Rs 25 Per Share' -> 25.

    A subject with several dividends ('Special Dividend - Rs 8 Per Share /Dividend - Rs 20 Per Share') returns their
    sum. Returns None for non-dividend actions (bonus, split, buyback) and for subjects without an amount.
    """
    amounts = _DIVIDEND.findall(subject or "")
    return sum((Decimal(a) for a in amounts), Decimal(0)) if amounts else None

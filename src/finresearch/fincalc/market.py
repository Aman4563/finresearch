"""Market arithmetic for listed stocks: returns, volatility, drawdowns, moving averages, dividends and ranges.

Price series are sequences of closes in chronological order (oldest first). Returns are fractions. Volatility
uses sample standard deviation of simple daily returns annualised by sqrt(periods_per_year) (252 trading days).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

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

    Returns None for non-dividend actions (bonus, split, buyback) and for subjects without an amount.
    """
    m = _DIVIDEND.search(subject or "")
    return Decimal(m.group(1)) if m else None

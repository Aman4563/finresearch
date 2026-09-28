"""Per-share and valuation arithmetic.

Units: ``pat``, ``net_worth``, ``amount``, ``market_cap``, ``debt``, ``cash`` must share one
rupee unit; per-share outputs are in that unit per share, so convert to **rupees** first
(``numbers.convert(x, "crore", "inr")``) when you want ₹/share. Multiples are plain numbers;
yields are fractions. ``None`` propagates; P/E and P/B return ``None`` for non-positive
EPS/BVPS (not meaningful). ValueError for negative share counts / prices and COE <= g.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal

from finresearch.fincalc.numbers import Num, opt_decimal, require_price, require_shares, to_decimal


def eps(pat: Num | None, shares: Num) -> Decimal | None:
    """``PAT / shares``. With PAT in rupees this is ₹/share.

    Note: RHP-printed EPS uses *weighted-average* shares (Ind AS 33); PAT / post-issue shares
    gives a "fully diluted post-issue" EPS, which is what an IPO P/E should use. Label which.
    """
    p = opt_decimal(pat)
    s = require_shares(shares)
    return None if p is None else p / s


def pe(price: Num, eps_: Num | None) -> Decimal | None:
    """``price / EPS``. None when EPS is None or <= 0 (loss-making: P/E not meaningful)."""
    pr = require_price(price)
    e = opt_decimal(eps_)
    if e is None or e <= 0:
        return None
    return pr / e


def bvps(net_worth: Num | None, shares: Num) -> Decimal | None:
    """Book value per share ``net worth / shares`` (net worth in rupees -> ₹/share)."""
    nw = opt_decimal(net_worth)
    return None if nw is None else nw / require_shares(shares)


def pb(price: Num, bvps_: Num | None) -> Decimal | None:
    """``price / BVPS``. None when BVPS is None or <= 0."""
    pr = require_price(price)
    b = opt_decimal(bvps_)
    if b is None or b <= 0:
        return None
    return pr / b


def market_cap(shares: Num, price: Num) -> Decimal:
    """``shares * price`` in rupees (use post-issue shares for IPO market cap)."""
    return require_shares(shares) * require_price(price)


def post_issue_shares(pre_shares: Num, fresh_shares: Num) -> Decimal:
    """``pre-issue shares + fresh shares``. OFS does not change the share count."""
    return require_shares(pre_shares) + require_shares(fresh_shares, "fresh_shares", allow_zero=True)


def shares_from_amount(amount: Num, price: Num, rounding: Literal["floor", "half_up"] = "floor") -> Decimal:
    """Shares issued for a rupee ``amount`` at ``price`` per share.

    Offer documents compute the fresh-issue share count as ``floor(amount / price)`` — you
    cannot issue a fraction of a share, and rounding up would exceed the authorised fresh
    issue size. E.g. ₹320 cr @ ₹272 = 11,764,705.88 -> **1,17,64,705** (Orient Cables RHP);
    ₹750 cr @ ₹34 = 220,588,235.29 -> 220,588,235 (Moneyview). ``amount`` must be in rupees.
    """
    a = to_decimal(amount)
    if a < 0:
        raise ValueError("amount must be >= 0")
    mode = {"floor": ROUND_DOWN, "half_up": ROUND_HALF_UP}[rounding]
    return (a / require_price(price)).quantize(Decimal(1), rounding=mode)


def ev(market_cap_: Num, debt: Num | None, cash: Num | None) -> Decimal | None:
    """Enterprise value ``market cap + debt - cash & equivalents``.

    For lenders/NBFCs EV is not meaningful (debt is raw material) — use P/B instead.
    """
    m, d, c = to_decimal(market_cap_), opt_decimal(debt), opt_decimal(cash)
    if d is None or c is None:
        return None
    return m + d - c


def ev_ebitda(ev_: Num | None, ebitda: Num | None) -> Decimal | None:
    """``EV / EBITDA``. None when EBITDA <= 0."""
    e, b = opt_decimal(ev_), opt_decimal(ebitda)
    if e is None or b is None or b <= 0:
        return None
    return e / b


def price_to_sales(market_cap_: Num, revenue: Num | None) -> Decimal | None:
    """``market cap / revenue`` (same unit). None when revenue <= 0."""
    m, r = to_decimal(market_cap_), opt_decimal(revenue)
    if r is None or r <= 0:
        return None
    return m / r


def earnings_yield(eps_: Num | None, price: Num) -> Decimal | None:
    """``EPS / price`` as a fraction (inverse of P/E; defined for losses too)."""
    e = opt_decimal(eps_)
    return None if e is None else e / require_price(price)


def justified_pb(roe: Num, cost_of_equity: Num, growth: Num) -> Decimal:
    """Gordon-growth justified P/B ``(ROE - g) / (COE - g)``; all inputs fractions.

    Raises ValueError unless ``COE > g`` (the model diverges otherwise).
    """
    r, k, g = to_decimal(roe), to_decimal(cost_of_equity), to_decimal(growth)
    if k <= g:
        raise ValueError("cost_of_equity must exceed growth")
    return (r - g) / (k - g)


@dataclass(frozen=True)
class FairValueRange:
    """Implied per-share value range from applying a multiple range to a per-share metric."""

    low: Decimal
    mid: Decimal
    high: Decimal
    metric: Decimal
    multiple_low: Decimal
    multiple_high: Decimal

    def upside_vs(self, price: Num) -> tuple[Decimal, Decimal, Decimal]:
        """Fractional up/downside of (low, mid, high) versus ``price``."""
        p = require_price(price)
        return (self.low / p - 1, self.mid / p - 1, self.high / p - 1)


def implied_fair_value(metric_per_share: Num, multiple_low: Num, multiple_high: Num) -> FairValueRange:
    """``metric x multiple`` range, e.g. BVPS x justified P/B band or EPS x peer P/E band.

    ``mid`` is the arithmetic midpoint. Raises ValueError if ``multiple_low > multiple_high`` or
    a multiple is negative.
    """
    m = to_decimal(metric_per_share)
    lo, hi = to_decimal(multiple_low), to_decimal(multiple_high)
    if lo < 0 or hi < 0 or lo > hi:
        raise ValueError("need 0 <= multiple_low <= multiple_high")
    return FairValueRange(m * lo, m * (lo + hi) / 2, m * hi, m, lo, hi)

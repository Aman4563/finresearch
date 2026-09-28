"""Financial ratios. All amounts must be in the SAME unit (e.g. all ₹ crore) and period.

Return conventions:

* Ratios/margins/returns are **fractions** (``0.12`` = 12%); multiples (D/E, coverage,
  turnover) are plain multiples; ``*_days`` are days.
* ``None`` is returned when any input is ``None`` (not reported) or when the ratio is
  undefined (zero / non-positive denominator). ValueError is reserved for structurally
  invalid inputs (e.g. non-positive ``days_in_period``).
"""

from __future__ import annotations

from decimal import Decimal

from finresearch.fincalc.numbers import Num, opt_decimal, to_decimal


def _div(num: Num | None, den: Num | None, *, positive_den: bool = False) -> Decimal | None:
    n, d = opt_decimal(num), opt_decimal(den)
    if n is None or d is None or d == 0 or (positive_den and d < 0):
        return None
    return n / d


def _avg(a: Num | None, b: Num | None) -> Decimal | None:
    x, y = opt_decimal(a), opt_decimal(b)
    if x is None or y is None:
        return None
    return (x + y) / 2


def _days(days_in_period: Num) -> Decimal:
    d = to_decimal(days_in_period)
    if d <= 0:
        raise ValueError("days_in_period must be > 0")
    return d


# --- margins -------------------------------------------------------------------------------


def gross_margin(revenue: Num | None, cogs: Num | None) -> Decimal | None:
    """``(revenue - COGS) / revenue``. COGS = materials consumed + purchases + change in inventory."""
    r, c = opt_decimal(revenue), opt_decimal(cogs)
    if r is None or c is None or r <= 0:
        return None
    return (r - c) / r


def ebitda_margin(ebitda: Num | None, revenue: Num | None) -> Decimal | None:
    """``EBITDA / revenue`` (revenue from operations; excludes other income unless stated)."""
    return _div(ebitda, revenue, positive_den=True)


def pat_margin(pat: Num | None, revenue: Num | None) -> Decimal | None:
    """``PAT / revenue``. Caller decides whether revenue is operations-only or total income."""
    return _div(pat, revenue, positive_den=True)


def ebitda(
    pbt: Num | None, finance_cost: Num | None, depreciation: Num | None, other_income: Num | None = 0
) -> Decimal | None:
    """``EBITDA = PBT + finance cost + D&A - other income`` (operating EBITDA).

    Pass ``other_income=0`` (default) to leave other income inside EBITDA.
    """
    p, f, d, o = (opt_decimal(v) for v in (pbt, finance_cost, depreciation, other_income))
    if p is None or f is None or d is None or o is None:
        return None
    return p + f + d - o


def ebit(pbt: Num | None, finance_cost: Num | None) -> Decimal | None:
    """``EBIT = PBT + finance cost`` (includes other income)."""
    p, f = opt_decimal(pbt), opt_decimal(finance_cost)
    if p is None or f is None:
        return None
    return p + f


# --- returns --------------------------------------------------------------------------------


def roe_on_closing_equity(pat: Num | None, closing_equity: Num | None) -> Decimal | None:
    """``PAT / closing net worth``. What RHPs usually print as "Return on Net Worth"."""
    return _div(pat, closing_equity, positive_den=True)


def roe_on_average_equity(
    pat: Num | None, opening_equity: Num | None, closing_equity: Num | None
) -> Decimal | None:
    """``PAT / ((opening + closing net worth) / 2)``. Analyst convention; after a large
    fresh raise within the year this is materially higher than the closing-equity ROE."""
    return _div(pat, _avg(opening_equity, closing_equity), positive_den=True)


def capital_employed(equity: Num | None, debt: Num | None, cash: Num | None = 0) -> Decimal | None:
    """``equity + total debt - cash & equivalents`` (pass ``cash=0`` for the gross variant)."""
    e, d, c = opt_decimal(equity), opt_decimal(debt), opt_decimal(cash)
    if e is None or d is None or c is None:
        return None
    return e + d - c


def roce(ebit_: Num | None, equity: Num | None, debt: Num | None, cash: Num | None) -> Decimal | None:
    """ROCE on net capital employed: ``EBIT / (equity + debt - cash)``."""
    return _div(ebit_, capital_employed(equity, debt, cash), positive_den=True)


def roce_gross(ebit_: Num | None, equity: Num | None, debt: Num | None) -> Decimal | None:
    """ROCE variant on gross capital employed: ``EBIT / (equity + debt)`` (cash not netted).
    Many RHP KPI tables use this form."""
    return _div(ebit_, capital_employed(equity, debt, 0), positive_den=True)


# --- leverage & coverage --------------------------------------------------------------------


def debt_to_equity(debt: Num | None, equity: Num | None) -> Decimal | None:
    """``total borrowings / net worth`` (multiple). None if equity <= 0."""
    return _div(debt, equity, positive_den=True)


def net_debt(debt: Num | None, cash: Num | None) -> Decimal | None:
    """``debt - cash & equivalents`` (negative = net cash)."""
    d, c = opt_decimal(debt), opt_decimal(cash)
    if d is None or c is None:
        return None
    return d - c


def net_debt_to_ebitda(debt: Num | None, cash: Num | None, ebitda_: Num | None) -> Decimal | None:
    """``(debt - cash) / EBITDA``. None if EBITDA <= 0 (ratio not meaningful)."""
    return _div(net_debt(debt, cash), ebitda_, positive_den=True)


def interest_coverage(ebit_: Num | None, finance_cost: Num | None) -> Decimal | None:
    """``EBIT / finance cost`` (multiple). None if finance cost is zero (no debt service)."""
    return _div(ebit_, finance_cost, positive_den=True)


def asset_turnover(revenue: Num | None, total_assets: Num | None) -> Decimal | None:
    """``revenue / total assets`` (multiple; pass average assets for the average variant)."""
    return _div(revenue, total_assets, positive_den=True)


# --- working capital ------------------------------------------------------------------------


def inventory_days(inventory: Num | None, cogs: Num | None, days_in_period: Num = 365) -> Decimal | None:
    """``inventory / COGS * days_in_period``. Use ``days_in_period`` = 91/92 for a quarter."""
    r = _div(inventory, cogs, positive_den=True)
    return None if r is None else r * _days(days_in_period)


def receivable_days(
    receivables: Num | None, revenue: Num | None, days_in_period: Num = 365
) -> Decimal | None:
    """``trade receivables / revenue * days_in_period`` (DSO)."""
    r = _div(receivables, revenue, positive_den=True)
    return None if r is None else r * _days(days_in_period)


def payable_days(
    payables: Num | None, purchases_or_cogs: Num | None, days_in_period: Num = 365
) -> Decimal | None:
    """``trade payables / purchases (or COGS) * days_in_period`` (DPO). State which base you used."""
    r = _div(payables, purchases_or_cogs, positive_den=True)
    return None if r is None else r * _days(days_in_period)


def cash_conversion_cycle(
    inv_days: Num | None, recv_days: Num | None, pay_days: Num | None
) -> Decimal | None:
    """``inventory days + receivable days - payable days``."""
    i, r, p = opt_decimal(inv_days), opt_decimal(recv_days), opt_decimal(pay_days)
    if i is None or r is None or p is None:
        return None
    return i + r - p


# --- cash flow ------------------------------------------------------------------------------


def cfo_to_ebitda(cfo: Num | None, ebitda_: Num | None) -> Decimal | None:
    """``cash from operations / EBITDA`` (cash conversion). None if EBITDA <= 0."""
    return _div(cfo, ebitda_, positive_den=True)


def fcf(cfo: Num | None, capex: Num | None) -> Decimal | None:
    """Free cash flow ``CFO - capex``. ``capex`` is the (positive) purchase of PP&E/intangibles;
    a negative value (as printed in the cash-flow statement) is taken as its absolute value."""
    c, k = opt_decimal(cfo), opt_decimal(capex)
    if c is None or k is None:
        return None
    return c - abs(k)

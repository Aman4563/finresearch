"""Growth arithmetic: % change, CAGR, annualisation and trailing-twelve-months.

All rates are returned as fractions (``0.25`` = 25%). ``None`` inputs propagate to ``None``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from finresearch.fincalc.numbers import Num, opt_decimal, to_decimal


def pct_change(old: Num | None, new: Num | None) -> Decimal | None:
    """Fractional change ``(new - old) / |old|``.

    Uses ``|old|`` so a move from a loss of -100 to -50 reads as +50% (improvement).
    Returns None if either input is None or ``old == 0`` (undefined).
    """
    o, n = opt_decimal(old), opt_decimal(new)
    if o is None or n is None or o == 0:
        return None
    return (n - o) / abs(o)


def cagr(start: Num | None, end: Num | None, years: Num) -> Decimal | None:
    """Compound annual growth rate ``(end / start) ** (1 / years) - 1`` as a fraction.

    ``years`` is the number of compounding periods (FY24 -> FY26 is 2 years, not 3).
    Returns None if an input is None or the sign makes CAGR meaningless (start <= 0 or end < 0).
    Raises ValueError if ``years <= 0``.
    """
    y = to_decimal(years)
    if y <= 0:
        raise ValueError("years must be > 0")
    s, e = opt_decimal(start), opt_decimal(end)
    if s is None or e is None or s <= 0 or e < 0:
        return None
    if e == 0:
        return Decimal(-1)
    with localcontext() as ctx:
        ctx.prec = 34
        return ((e / s).ln() / y).exp() - 1


@dataclass(frozen=True)
class Annualised:
    """A run-rate figure. ``annualised=True`` means it is a mechanical scale-up of a
    partial period (e.g. Q1 x 4), **not** a forecast, and must be labelled as such."""

    value: Decimal
    months: int
    annualised: bool
    label: str


def annualise(value: Num, months: int) -> Annualised:
    """Scale a ``months``-month flow figure to 12 months: ``value * 12 / months``.

    A 3-month figure x4 is flagged ``annualised=True`` ("annualised, not a forecast"; ignores
    seasonality). A 12-month input is returned unchanged with ``annualised=False``.
    Only valid for flow items (revenue, PAT), not balance-sheet items.
    """
    if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= 12:
        raise ValueError("months must be an int in 1..12")
    v = to_decimal(value)
    if months == 12:
        return Annualised(v, 12, False, "reported 12M")
    return Annualised(v * 12 / months, months, True, f"annualised from {months}M (not a forecast)")


def ttm(quarters: Sequence[Num | None]) -> Decimal | None:
    """Trailing twelve months = sum of the last four quarterly flow figures.

    ``quarters`` must be exactly 4 values. Returns None if any is None (never treats a missing
    quarter as zero).
    """
    if len(quarters) != 4:
        raise ValueError("ttm needs exactly 4 quarters")
    vals = [opt_decimal(q) for q in quarters]
    if any(v is None for v in vals):
        return None
    return sum((v for v in vals if v is not None), Decimal(0))


def ttm_from_fy(fy: Num | None, current_ytd: Num | None, prior_ytd: Num | None) -> Decimal | None:
    """TTM via ``FY + current YTD - same period last year`` (e.g. FY26 + Q1FY27 - Q1FY26)."""
    f, c, p = opt_decimal(fy), opt_decimal(current_ytd), opt_decimal(prior_ytd)
    if f is None or c is None or p is None:
        return None
    return f + c - p

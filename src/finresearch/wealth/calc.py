"""Valuation formulas for manual assets and loans (Decimal; golden-tested in tests/test_wealth_calc.py).

- Fixed deposit (cumulative): A = P (1 + r/m)^(m t), t in years = days / 365, stopped at maturity. Quarterly
  compounding (m = 4) is the usual bank convention [unverified: check the FD advice]. m = 0 means interest is paid
  out, so the deposit is worth its principal.
- Recurring deposit: each monthly instalment D compounds quarterly from the month it is paid:
  value = sum_k D (1 + r/4)^(4 * months_k / 12), the formula banks publish for RDs [unverified].
- EPF / PPF: the last entered balance plus monthly contributions, interest at the stated rate computed monthly on
  the running balance and credited once a year (every 12 months from the balance date). EPFO: "compound interest
  is credited on monthly running balance basis at the statutory rate declared for each year" (epfo.gov.in FAQ, read
  30-Sep-2026). Rates are user-entered; the defaults in RATE_DEFAULTS are [unverified].
- Loan EMI: EMI = P i (1+i)^n / ((1+i)^n - 1), i = annual rate / 12.
- Outstanding after k EMIs: B_k = P (1+i)^k - EMI ((1+i)^k - 1) / i.
- Months left at a fixed EMI after a prepayment: n = -ln(1 - B i / EMI) / ln(1 + i).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal(0)

# Dated default rates offered when adding an asset. The user's entered rate always wins.
RATE_DEFAULTS: dict[str, dict[str, str]] = {
    "epf": {
        "rate_pct": "8.25",
        "as_of": "FY 2024-25",
        "note": "EPF rate declared for FY 2023-24 and 2024-25 [unverified: check epfo.gov.in circulars]",
    },
    "ppf": {
        "rate_pct": "7.1",
        "as_of": "2026 Q3",
        "note": "PPF rate set quarterly by the Ministry of Finance [unverified: check nsiindia.gov.in]",
    },
}


def D(x: object) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def months_between(start: date, end: date) -> int:
    """Whole calendar months from start to end (0 if end is before start or inside the first month)."""
    m = (end.year - start.year) * 12 + end.month - start.month - (1 if end.day < start.day else 0)
    return max(m, 0)


# --------------------------------------------------------------------------- deposits
def fd_value(
    principal: object,
    rate_pct: object,
    start: date,
    on: date,
    *,
    compounding: int = 4,
    maturity: date | None = None,
) -> Decimal:
    """Value of a cumulative FD on `on`: P (1 + r/m)^(m t). Before the start date: 0 (it did not exist)."""
    p, r = D(principal), D(rate_pct) / 100
    if on < start:
        return ZERO
    if compounding <= 0:
        return money(p)
    end = min(on, maturity) if maturity else on
    t = D((end - start).days) / 365
    return money(p * (1 + r / compounding) ** (compounding * t))


def rd_value(
    monthly: object, rate_pct: object, start: date, on: date, *, maturity: date | None = None
) -> Decimal:
    """Recurring deposit: instalments on the start day of each month up to `on` (or maturity), each compounding
    quarterly for the months it has been in."""
    d, r = D(monthly), D(rate_pct) / 100
    if on < start:
        return ZERO
    end = min(on, maturity) if maturity else on
    n = months_between(start, end) + 1  # the first instalment is paid on the start date
    if maturity and end == maturity:
        n = months_between(start, maturity)  # tenure: the last instalment is a month before maturity
    total = ZERO
    for k in range(n):
        months_in = months_between(start, end) - k
        total += d * (1 + r / 4) ** (D(4 * months_in) / 12)
    return money(total)


def provident_value(
    balance: object, rate_pct: object, balance_day: date, on: date, *, monthly: object = 0
) -> Decimal:
    """EPF/PPF-style accrual from a known balance: contributions at each month end, interest on the monthly running
    balance at rate/12, credited every 12 months from the balance date; uncredited interest is included pro rata (an
    estimate of the value today, not a statement figure)."""
    bal, r, c = D(balance), D(rate_pct) / 100, D(monthly)
    if on <= balance_day:
        return money(bal)
    pending = ZERO
    for m in range(1, months_between(balance_day, on) + 1):
        pending += bal * r / 12
        bal += c
        if m % 12 == 0:
            bal += pending
            pending = ZERO
    return money(bal + pending)


# --------------------------------------------------------------------------- loans
def emi(principal: object, rate_pct: object, months: int) -> Decimal:
    p, i = D(principal), D(rate_pct) / 1200
    if months <= 0:
        raise ValueError("tenure must be at least one month")
    if i == 0:
        return money(p / months)
    f = (1 + i) ** months
    return money(p * i * f / (f - 1))


def outstanding(principal: object, rate_pct: object, emi_amount: object, k: int) -> Decimal:
    """B_k = P(1+i)^k - EMI((1+i)^k - 1)/i, floored at 0 (the loan is repaid)."""
    p, i, e = D(principal), D(rate_pct) / 1200, D(emi_amount)
    if k <= 0:
        return money(p)
    if i == 0:
        return money(max(p - e * k, ZERO))
    f = (1 + i) ** k
    return money(max(p * f - e * (f - 1) / i, ZERO))


def months_to_repay(balance: object, rate_pct: object, emi_amount: object) -> float:
    """Months left at a fixed EMI: n = -ln(1 - B i / EMI) / ln(1 + i). inf if the EMI does not cover interest."""
    b, i, e = float(balance), float(rate_pct) / 1200, float(emi_amount)
    if b <= 0:
        return 0.0
    if i == 0:
        return b / e
    x = 1 - b * i / e
    return math.inf if x <= 0 else -math.log(x) / math.log(1 + i)


@dataclass(frozen=True)
class Prepayment:
    amount: Decimal
    months_before: float
    months_after: float
    interest_before: Decimal
    interest_after: Decimal

    @property
    def interest_saved(self) -> Decimal:
        return self.interest_before - self.interest_after

    @property
    def months_saved(self) -> float:
        return self.months_before - self.months_after


def prepayment(balance: object, rate_pct: object, emi_amount: object, amount: object) -> Prepayment:
    """Prepay `amount` now and keep the EMI (tenure shortens). Remaining interest = EMI x months - balance, with the
    fractional last month counted pro rata (the closed form of the schedule)."""
    b, e, a = D(balance), D(emi_amount), min(D(amount), D(balance))
    n0 = months_to_repay(b, rate_pct, e)
    n1 = months_to_repay(b - a, rate_pct, e)
    i0 = money(e * D(n0) - b) if math.isfinite(n0) else ZERO
    i1 = money(e * D(n1) - (b - a)) if math.isfinite(n1) else ZERO
    return Prepayment(a, n0, n1, i0, i1)

"""Bond arithmetic for Indian listed bonds and NCDs: cash flows, price from yield, yield to maturity, accrued
interest, durations, convexity and post-tax yield.

Conventions (state them in reports):
* Coupons are paid `freq` times a year (1 annual, 2 half-yearly, 4 quarterly, 12 monthly) on the maturity date's
  day and month schedule, the last one with the principal.
* Discounting counts coupon periods: the fraction of the current period still to run (actual days / actual days in
  the period), then whole periods, so a bond priced at par on a coupon date yields exactly its coupon.
* Accrued interest is Actual/Actual as SEBI prescribes for listed debt: a day accrues coupon/365, or coupon/366 in a
  one-year period that contains 29 February.
* Yields are annual, compounded `freq` times a year; prices are per `face` value and exclude accrued interest (clean)
  unless named dirty.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finresearch.fincalc.numbers import Num, require_price, to_decimal


def _months(freq: int) -> int:
    if freq not in (1, 2, 4, 12):
        raise ValueError("freq must be 1, 2, 4 or 12")
    return 12 // freq


def _back(d: date, months: int) -> date:
    """`d` moved back by `months` calendar months, clamped to month end."""
    y, m = divmod(d.month - 1 - months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def coupon_dates(settlement: date, maturity: date, freq: int) -> list[date]:
    """Coupon dates after `settlement` up to and including `maturity`, stepping back from maturity."""
    if maturity <= settlement:
        raise ValueError("maturity must be after settlement")
    step, out, k = _months(freq), [], 0
    while (d := _back(maturity, k * step)) > settlement:
        out.append(d)
        k += 1
    return sorted(out)


def previous_coupon(settlement: date, maturity: date, freq: int) -> date:
    first = coupon_dates(settlement, maturity, freq)[0]
    return _back(first, _months(freq))


@dataclass(frozen=True)
class CashFlow:
    day: date
    amount: Decimal
    periods: Decimal  # coupon periods from settlement (fractional first period)

    def years(self, freq: int) -> Decimal:
        return self.periods / freq


def cash_flows(
    settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100
) -> list[CashFlow]:
    f, c = require_price(face, "face"), to_decimal(coupon_rate)
    per = f * c / freq
    days = coupon_dates(settlement, maturity, freq)
    prev = _back(days[0], _months(freq))
    first = Decimal((days[0] - settlement).days) / Decimal((days[0] - prev).days)
    return [CashFlow(d, per + (f if d == maturity else 0), first + k) for k, d in enumerate(days)]


def _anniversary(maturity: date, year: int) -> date:
    return date(year, maturity.month, min(maturity.day, calendar.monthrange(year, maturity.month)[1]))


def _year_basis(d: date, maturity: date) -> int:
    """366 when the one-year period between two anniversaries of the maturity date that contains `d` includes a
    29 February, else 365 (SEBI day-count convention, CIR/IMD/DF-1/122/2016, carried into the NCS operational
    circular)."""
    end = _anniversary(maturity, d.year)
    if end <= d:
        end = _anniversary(maturity, d.year + 1)
    start = _anniversary(maturity, end.year - 1)
    leap_days = (date(y, 2, 29) for y in range(start.year, end.year + 1) if calendar.isleap(y))
    return 366 if any(start < x <= end for x in leap_days) else 365


def accrued_interest(
    settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100
) -> Decimal:
    """Coupon accrued since the previous coupon date on the SEBI Actual/Actual basis: each day accrues
    coupon / 365, or coupon / 366 in a one-year period that contains 29 February."""
    prev = previous_coupon(settlement, maturity, freq)
    per_year = require_price(face, "face") * to_decimal(coupon_rate)
    total, d = Decimal(0), prev
    while d < settlement:
        total += per_year / _year_basis(d, maturity)
        d = date.fromordinal(d.toordinal() + 1)
    return total


def dirty_price(
    yld: Num, settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100
) -> Decimal:
    """Present value of the remaining cash flows at annual yield `yld` compounded `freq` times a year."""
    base = 1 + float(to_decimal(yld)) / freq
    total = sum(float(cf.amount) / base ** float(cf.periods)
                for cf in cash_flows(settlement, maturity, coupon_rate, freq, face))  # fmt: skip
    return Decimal(str(total))


def clean_price(
    yld: Num, settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100
) -> Decimal:
    return dirty_price(yld, settlement, maturity, coupon_rate, freq, face) - accrued_interest(
        settlement, maturity, coupon_rate, freq, face)  # fmt: skip


def ytm(
    clean: Num, settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100
) -> Decimal:
    """Yield to maturity for a clean price (bisection between -50% and 100%). Raises ValueError when the price is
    outside the prices those two yields give (a wrong price basis or face value, a stale quote): bisection would
    otherwise return the bound itself, e.g. a "100 % YTM" for a price of 10 on a 9 % bond."""
    target = require_price(clean, "price")
    lo, hi = Decimal("-0.5"), Decimal("1.0")
    if not clean_price(hi, settlement, maturity, coupon_rate, freq, face) <= target <= clean_price(
            lo, settlement, maturity, coupon_rate, freq, face):  # fmt: skip
        raise ValueError(f"no yield between -50 % and 100 % gives the clean price {target}")
    for _ in range(120):
        mid = (lo + hi) / 2
        if clean_price(mid, settlement, maturity, coupon_rate, freq, face) > target:
            lo = mid
        else:
            hi = mid
    return ((lo + hi) / 2).quantize(Decimal("1e-8"))


def current_yield(clean: Num, coupon_rate: Num, face: Num = 100) -> Decimal:
    return require_price(face, "face") * to_decimal(coupon_rate) / require_price(clean, "price")


@dataclass(frozen=True)
class Duration:
    macaulay: Decimal  # years
    modified: Decimal  # years; % price change for a 1-point yield change is about -modified
    convexity: Decimal


def duration(
    yld: Num, settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100
) -> Duration:
    y = float(to_decimal(yld))
    flows = cash_flows(settlement, maturity, coupon_rate, freq, face)
    pv = [(float(cf.amount) / (1 + y / freq) ** float(cf.periods), float(cf.periods) / freq) for cf in flows]
    price = sum(v for v, _ in pv)
    mac = sum(v * t for v, t in pv) / price
    conv = sum(v * t * (t + 1 / freq) for v, t in pv) / (price * (1 + y / freq) ** 2)
    return Duration(
        Decimal(str(round(mac, 6))),
        Decimal(str(round(mac / (1 + y / freq), 6))),
        Decimal(str(round(conv, 6))),
    )


def post_tax_yield(pre_tax_yield: Num, tax_rate: Num) -> Decimal:
    """Rough yield after tax, ``YTM x (1 - tax_rate)``. Right only for a bond bought at par: it taxes the whole
    return as interest. Use `after_tax_ytm` for bonds bought at a premium or discount."""
    return to_decimal(pre_tax_yield) * (1 - to_decimal(tax_rate))


def after_tax_ytm(clean: Num, settlement: date, maturity: date, coupon_rate: Num, freq: int, tax_rate: Num,
                  capital_gains_rate: Num | None = None, face: Num = 100, loss_offset: bool = False,
                  accrued: Num = 0) -> Decimal:  # fmt: skip
    """Yield on after-tax cash flows: each coupon is taxed at `tax_rate` (the investor's slab), and the difference
    between face value and the clean purchase price is a capital gain or loss at redemption, taxed at
    `capital_gains_rate` (default: the same as `tax_rate`). A capital loss saves tax only when `loss_offset` is
    True (the investor has gains to set it against).

    `accrued` is the interest accrued since the last coupon, which the buyer pays on top of the clean price (NSE
    trades bonds on the dirty price). Pass it whenever settlement is between coupon dates: the first coupon returns
    it, so leaving it out overstates the yield (an 8.98 % bond 2.45 years from maturity with 49.45 accrued on a clean
    1026.55 comes out 6.50 % instead of 4.32 % after 31.2 % tax). The whole first coupon is taxed, a conservative
    simplification: relief for the purchased broken-period interest is not assumed."""
    t = float(to_decimal(tax_rate))
    cg = float(to_decimal(capital_gains_rate)) if capital_gains_rate is not None else t
    price, f = float(require_price(clean, "price")), float(require_price(face, "face"))
    paid = price + float(to_decimal(accrued))
    flows = cash_flows(settlement, maturity, coupon_rate, freq, face)
    coupon = f * float(to_decimal(coupon_rate)) / freq
    gain = f - price
    gain_tax = gain * cg if gain > 0 or loss_offset else 0.0
    after = [
        (float(cf.periods), coupon * (1 - t) + ((f - gain_tax) if cf.day == maturity else 0.0))
        for cf in flows
    ]

    def pv(y: float) -> float:
        return sum(a / (1 + y / freq) ** p for p, a in after)

    lo, hi = -0.5, 1.0
    if not pv(hi) <= paid <= pv(lo):
        raise ValueError(f"no after-tax yield between -50 % and 100 % gives the price paid {paid:g}")
    for _ in range(200):
        mid = (lo + hi) / 2
        if pv(mid) > paid:
            lo = mid
        else:
            hi = mid
    return Decimal(str(round((lo + hi) / 2, 8)))


def effective_annual(nominal: Num, freq: int) -> Decimal:
    """A yield compounded `freq` times a year as an effective annual rate, ``(1 + y/freq)**freq - 1``. Compare yields
    only on this basis: a half-yearly 6.66 % G-sec par yield is 6.77 % a year, a quarterly-compounded 6.40 % FD is
    6.56 %."""
    if freq < 1:
        raise ValueError("freq must be at least 1")
    y = float(to_decimal(nominal))
    return Decimal(str(round((1 + y / freq) ** freq - 1, 10)))


def after_tax_par_yield(nominal: Num, tax_rate: Num, freq: int) -> Decimal:
    """Effective annual yield after tax on an instrument bought at par (a par G-sec, an FD): each interest payment is
    taxed at `tax_rate`, so the post-tax nominal yield is ``y x (1 - t)``, then made effective annual."""
    return effective_annual(post_tax_yield(nominal, tax_rate), freq)

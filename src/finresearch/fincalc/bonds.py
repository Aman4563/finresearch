"""Bond arithmetic for Indian listed bonds and NCDs: cash flows, price from yield, yield to maturity, accrued
interest, durations, convexity and post-tax yield.

Conventions (state them in reports):
* Coupons are paid `freq` times a year (1 annual, 2 half-yearly, 4 quarterly, 12 monthly) on the maturity date's
  day and month schedule, the last one with the principal. Dates are the due dates, with no holiday shift. In SEBI's
  master circular (Chapter III, Table 1) a coupon moved to the next working day, and a redemption moved to the
  previous one, keep the amount of the due date; some offer documents instead pay the last coupon only up to an
  early redemption day. For trades on the RFQ platform the same circular (Chapter XXII, para 9) has yield computed
  on "the due date of payment as per the cash flow schedule and not as per the date of payment".
* Interest follows the instrument's own day count, `day_count`, for the coupon paid and the interest accrued alike
  (#519: the two used to disagree for listed bonds, which accrued by the day and paid equal coupons):
  - `SEBI` (the default), listed NCDs and bonds: Actual/Actual. A coupon is face x rate x the actual days of its
    period / 365, or / 366 for every period of a coupon year that holds 29 February, so half-yearly, quarterly and
    monthly payments differ (181 against 184 days) and add up to the annual coupon over a coupon year. Sources, read
    on sebi.gov.in on 23-Nov-2026: master circular SEBI/HO/DDHS/DDHS-PoD/P/CIR/2025/0000000137 of 15-Oct-2025,
    Chapter III ("366 days ... for the entire year, irrespective of whether the interest/ dividend is payable
    annually, half yearly, quarterly or monthly", and Table 1); CIR/IMD/DF-1/122/2016, clause 3(b) ("for a half
    yearly interest payment, 366 days would be reckoned twice as the denominator"); CIR/IMD/DF/18/2013, Annexure A
    (amount = face x rate x "No. of days in Coupon Period" / 365 or 366). SEBI prints no half-yearly table: the
    unequal amounts follow from that wording and are shown in numbers by issuers' offer documents. The coupon year
    runs between anniversaries of the maturity (SEBI's examples count it from the allotment date, the same thing
    for a tenor of whole years). Bonds issued before 1-Jan-2017 follow their own offer document in a leap year and
    when a holiday moves a payment (the 2013 annexure paid the extra day).
  - `GSEC`, dated Government securities and State Development Loans: equal coupons, "half yearly payment being half
    of the annual coupon", and 30/360 accrual (RBI, "Government Securities Market in India - A Primer",
    https://www.rbi.org.in/Scripts/FAQView.aspx?Id=79, read 23-Nov-2026). Not for T-bills (actual/365, no coupon)
    or Sovereign Gold Bonds (RBI states "half-yearly rests" and no day count).
* Discounting counts coupon periods: the fraction of the current period still to run (actual days / actual days in
  the period), then whole periods, so a bond with equal coupons (a G-sec; a listed bond paying once a year) priced
  at par on a coupon date yields exactly its coupon.
* Yields are annual, compounded `freq` times a year; prices are per `face` value and exclude accrued interest (clean)
  unless named dirty. Amounts are not rounded (the 2013 annexure rounds each payment to the nearest rupee).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finresearch.fincalc.numbers import Num, require_price, to_decimal

# Day-count rules, one per instrument class (module docstring). Never mix them: on the same dates they differ.
SEBI = "actual/actual"  # listed NCDs and bonds (SEBI's NCS master circular, Chapter III)
GSEC = "30/360"  # dated Government securities and SDLs (RBI's primer, questions 22 and 25)
DAY_COUNTS = (SEBI, GSEC)


def _rule(day_count: str) -> str:
    if day_count not in DAY_COUNTS:
        raise ValueError(f"day_count must be one of {DAY_COUNTS}")
    return day_count


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
    """The coupon date on or before `settlement`. Like every date of the schedule it is stepped back from the
    maturity date itself, not from the next coupon: a month-end maturity's next coupon may be clamped (31-Aug ->
    28-Feb), and one period back from the clamped date is 28-Aug, three days short of the 31-Aug the bond pays on."""
    return _back(maturity, len(coupon_dates(settlement, maturity, freq)) * _months(freq))


@dataclass(frozen=True)
class CashFlow:
    day: date
    amount: Decimal  # the coupon, plus the face value on the maturity date
    periods: Decimal  # coupon periods from settlement (fractional first period)
    coupon: Decimal  # the interest part of `amount`

    def years(self, freq: int) -> Decimal:
        return self.periods / freq


def cash_flows(settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100,
               day_count: str = SEBI) -> list[CashFlow]:  # fmt: skip
    """The payments after `settlement`. Each coupon is the interest of its whole period under `day_count`: for a
    listed bond the actual days of the period over the coupon year's 365 or 366 (exactly what `accrued_interest`
    reaches on the coupon date), for a Government security the annual coupon / freq."""
    f, c = require_price(face, "face"), to_decimal(coupon_rate)
    step, days = _months(freq), coupon_dates(settlement, maturity, freq)
    # each period's start, stepped back from the maturity like the dates themselves (previous_coupon)
    starts = [_back(maturity, (len(days) - k) * step) for k in range(len(days))]
    first = Decimal((days[0] - settlement).days) / Decimal((days[0] - starts[0]).days)
    equal = f * c / freq if _rule(day_count) == GSEC else None
    out = []
    for k, (start, d) in enumerate(zip(starts, days, strict=True)):
        coupon = equal if equal is not None else _actual_interest(start, d, maturity, f * c)
        out.append(CashFlow(d, coupon + (f if d == maturity else 0), first + k, coupon))
    return out


def _anniversary(maturity: date, year: int) -> date:
    return date(year, maturity.month, min(maturity.day, calendar.monthrange(year, maturity.month)[1]))


def _year_end(d: date, maturity: date) -> date:
    """The first anniversary of the maturity date after `d`: the end of the coupon year `d` accrues in."""
    end = _anniversary(maturity, d.year)
    return end if end > d else _anniversary(maturity, d.year + 1)


def _year_basis(d: date, maturity: date) -> int:
    """366 when the one-year period between two anniversaries of the maturity date that contains `d` includes a
    29 February, else 365 (SEBI day-count convention: CIR/IMD/DF-1/122/2016, clause 3(b), now Chapter III, para 4 of
    the NCS master circular of 15-Oct-2025, whose Table 1 gives 366 to the coupon year 14-Dec-2023..14-Dec-2024)."""
    end = _year_end(d, maturity)
    start = _anniversary(maturity, end.year - 1)
    leap_days = (date(y, 2, 29) for y in range(start.year, end.year + 1) if calendar.isleap(y))
    return 366 if any(start < x <= end for x in leap_days) else 365


def _actual_interest(start: date, end: date, maturity: date, per_year: Decimal) -> Decimal:
    """SEBI Actual/Actual interest for the days from `start` up to but not including `end`: each day earns
    per_year / 365, or / 366 in a coupon year that holds 29 February (summed one coupon year at a time)."""
    total, d = Decimal(0), start
    while d < end:
        stop = min(end, _year_end(d, maturity))
        total += per_year * (stop - d).days / _year_basis(d, maturity)
        d = stop
    return total


def _days_30_360(start: date, end: date) -> int:
    """Days from `start` to `end` with every month taken as 30 days and a 31st as the 30th: RBI's primer, question
    25 ("irrespective of the actual number of days in a month, the number of days in a month is taken as 30"), and
    its Annex 6, which names Excel's basis 4 (the European 30/360) for Government bonds."""
    return (
        (end.year - start.year) * 360 + (end.month - start.month) * 30 + min(end.day, 30) - min(start.day, 30)
    )


def interest(start: date, end: date, maturity: date, coupon_rate: Num, face: Num = 100,
             day_count: str = SEBI) -> Decimal:  # fmt: skip
    """Interest earned from `start` up to but not including `end` under the instrument's day count. `maturity` fixes
    the coupon years of a listed bond (the 365 or 366). Use it for a period the schedule does not give: one a
    holiday stretched under a pre-2017 offer document, or an issuer's own first or last period."""
    per_year = require_price(face, "face") * to_decimal(coupon_rate)
    if _rule(day_count) == GSEC:
        return per_year * _days_30_360(start, end) / 360
    return _actual_interest(start, end, maturity, per_year)


def accrued_interest(settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100,
                     day_count: str = SEBI) -> Decimal:  # fmt: skip
    """Coupon accrued since the previous coupon date. Listed bonds (`SEBI`): Actual/Actual, each day accrues
    coupon / 365, or coupon / 366 in a one-year period that contains 29 February; on the next coupon date it has
    reached the coupon `cash_flows` pays. Government securities (`GSEC`): 30/360, RBI's worked case being
    8.83 x 65/360 = 1.5943 per 100 from 25-Nov-2013 to settlement on 30-Jan-2014."""
    return interest(
        previous_coupon(settlement, maturity, freq), settlement, maturity, coupon_rate, face, day_count
    )


def dirty_price(yld: Num, settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100,
                day_count: str = SEBI) -> Decimal:  # fmt: skip
    """Present value of the remaining cash flows at annual yield `yld` compounded `freq` times a year."""
    base = 1 + float(to_decimal(yld)) / freq
    total = sum(float(cf.amount) / base ** float(cf.periods)
                for cf in cash_flows(settlement, maturity, coupon_rate, freq, face, day_count))  # fmt: skip
    return Decimal(str(total))


def clean_price(yld: Num, settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100,
                day_count: str = SEBI) -> Decimal:  # fmt: skip
    return dirty_price(yld, settlement, maturity, coupon_rate, freq, face, day_count) - accrued_interest(
        settlement, maturity, coupon_rate, freq, face, day_count)  # fmt: skip


def ytm(clean: Num, settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100,
        day_count: str = SEBI) -> Decimal:  # fmt: skip
    """Yield to maturity for a clean price (bisection between -50% and 100%). Raises ValueError when the price is
    outside the prices those two yields give (a wrong price basis or face value, a stale quote): bisection would
    otherwise return the bound itself, e.g. a "100 % YTM" for a price of 10 on a 9 % bond."""
    target = require_price(clean, "price")
    lo, hi = Decimal("-0.5"), Decimal("1.0")
    if not clean_price(hi, settlement, maturity, coupon_rate, freq, face, day_count) <= target <= clean_price(
            lo, settlement, maturity, coupon_rate, freq, face, day_count):  # fmt: skip
        raise ValueError(f"no yield between -50 % and 100 % gives the clean price {target}")
    for _ in range(120):
        mid = (lo + hi) / 2
        if clean_price(mid, settlement, maturity, coupon_rate, freq, face, day_count) > target:
            lo = mid
        else:
            hi = mid
    return ((lo + hi) / 2).quantize(Decimal("1e-8")) + 0  # + 0: a zero yield is 0, never -0 ("-0.00%")


def current_yield(clean: Num, coupon_rate: Num, face: Num = 100) -> Decimal:
    return require_price(face, "face") * to_decimal(coupon_rate) / require_price(clean, "price")


@dataclass(frozen=True)
class Duration:
    macaulay: Decimal  # years
    modified: Decimal  # years; % price change for a 1-point yield change is about -modified
    convexity: Decimal


def duration(yld: Num, settlement: date, maturity: date, coupon_rate: Num, freq: int, face: Num = 100,
             day_count: str = SEBI) -> Duration:  # fmt: skip
    y = float(to_decimal(yld))
    flows = cash_flows(settlement, maturity, coupon_rate, freq, face, day_count)
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
                  accrued: Num = 0, day_count: str = SEBI) -> Decimal:  # fmt: skip
    """Yield on after-tax cash flows: each coupon (its own amount under `day_count`: a listed bond's payments differ
    with the days of each period) is taxed at `tax_rate` (the investor's slab), and the difference
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
    flows = cash_flows(settlement, maturity, coupon_rate, freq, face, day_count)
    gain = f - price
    gain_tax = gain * cg if gain > 0 or loss_offset else 0.0
    after = [
        (float(cf.periods), float(cf.coupon) * (1 - t) + ((f - gain_tax) if cf.day == maturity else 0.0))
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
    return Decimal(str(round((lo + hi) / 2, 8) + 0.0))  # + 0.0: never -0.0


def effective_annual(nominal: Num, freq: int) -> Decimal:
    """A yield compounded `freq` times a year as an effective annual rate, ``(1 + y/freq)**freq - 1``. Compare yields
    only on this basis: a half-yearly 7.00 % G-sec par yield is 7.12 % a year, a quarterly-compounded 6.40 % FD is
    6.56 %."""
    if freq < 1:
        raise ValueError("freq must be at least 1")
    y = float(to_decimal(nominal))
    return Decimal(str(round((1 + y / freq) ** freq - 1, 10)))


def after_tax_par_yield(nominal: Num, tax_rate: Num, freq: int) -> Decimal:
    """Effective annual yield after tax on an instrument bought at par (a par G-sec, an FD): each interest payment is
    taxed at `tax_rate`, so the post-tax nominal yield is ``y x (1 - t)``, then made effective annual."""
    return effective_annual(post_tax_yield(nominal, tax_rate), freq)

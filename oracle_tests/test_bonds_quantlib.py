"""fincalc/bonds.py against QuantLib's FixedRateBond, BondFunctions and CashFlows.

Conventions pinned on the QuantLib side to match the module's docstring before any difference counts:
* schedule: rolled BACK from the maturity date in steps of 12/freq months, each date taken from the maturity itself
  and clamped to the month's end, no business-day adjustment, end-of-month rule off, no settlement lag, face 100;
* discounting: `Compounded` at `freq`, time by `ActualActual(ISMA, schedule)` (the fraction of the current coupon
  period still to run, then whole periods): the street convention the module's "counts coupon periods" describes;
* accrued interest: NOT a QuantLib day count. The module follows SEBI's rule as its own docstring and tests state it
  (CIR/IMD/DF-1/122/2016): when 29 February falls in a one-year period, 366 is the denominator for that whole year,
  whatever the coupon frequency. It is rebuilt here from QuantLib's own dates and day counts, and the gap to
  ActualActual(ISMA) is measured and explained.

The grid is built to hit what tests/test_bonds.py never did: settlement between coupon dates, on the eve and the day
of a coupon, on and around 29 February, month-end maturities (29th, 30th, 31st), and all four frequencies.
"""

from __future__ import annotations

import itertools
import math
from datetime import date, timedelta
from decimal import Decimal

import pytest
import QuantLib as ql

from finresearch.fincalc import bonds as b

FREQ = {1: ql.Annual, 2: ql.Semiannual, 4: ql.Quarterly, 12: ql.Monthly}
MATURITIES = [
    date(2031, 1, 1), date(2028, 3, 15), date(2029, 6, 28),  # a day every month has
    date(2030, 3, 31), date(2030, 8, 31), date(2027, 12, 31), date(2033, 5, 31),  # the 31st
    date(2029, 11, 30), date(2031, 4, 30),  # the 30th
    date(2032, 2, 29), date(2029, 8, 29), date(2030, 2, 28),  # 29 February, a 29th, and 28 February
]  # fmt: skip
SETTLEMENTS = [
    date(2024, 1, 15), date(2024, 2, 28), date(2024, 2, 29), date(2024, 3, 1), date(2025, 8, 31),
    date(2026, 1, 1), date(2026, 3, 17), date(2026, 9, 30), date(2026, 10, 1), date(2026, 11, 23),
    date(2027, 2, 28), date(2027, 3, 1), date(2027, 8, 30), date(2027, 12, 30), date(2028, 2, 29),
    date(2028, 3, 1), date(2028, 3, 14),
]  # fmt: skip
COUPONS = ["0", "0.0675", "0.09", "0.1425"]
YIELDS = ["0.02", "0.0735", "0.11", "0.25"]


def qd(d: date) -> ql.Date:
    return ql.Date(d.day, d.month, d.year)


def pd(d: ql.Date) -> date:
    return date(d.year(), d.month(), d.dayOfMonth())


def schedule(settle: date, maturity: date, freq: int) -> ql.Schedule:
    """QuantLib's backward schedule, started on its own n-th date before maturity so that no period is a stub."""
    cal, n = ql.NullCalendar(), 0
    while True:
        n += 1
        start = cal.advance(qd(maturity), ql.Period(-n * (12 // freq), ql.Months), ql.Unadjusted, False)
        if start <= qd(settle):
            break
    return ql.Schedule(start, qd(maturity), ql.Period(FREQ[freq]), cal, ql.Unadjusted, ql.Unadjusted,
                       ql.DateGeneration.Backward, False)  # fmt: skip


def cases(coupons=COUPONS, yields=YIELDS):
    for mat, st, c, f, y in itertools.product(MATURITIES, SETTLEMENTS, coupons, FREQ, yields):
        if st < mat:
            yield mat, st, c, f, y


def street(sched: ql.Schedule, freq: int, c: str, y: str):
    dc = ql.ActualActual(ql.ActualActual.ISMA, sched)
    return ql.FixedRateBond(0, 100.0, sched, [float(c)], dc), ql.InterestRate(
        float(y), dc, ql.Compounded, FREQ[freq]
    )


def test_coupon_schedule_matches_quantlib(oracle):
    """Dates, so the tolerance is zero. The previous coupon date is the one the accrual and the first discounting
    fraction both start from."""
    wrong, n = [], 0
    for mat, st, f in {(m, s, f) for m, s, _, f, _ in cases(["0"], ["0"])}:
        n += 1
        days = [pd(d) for d in schedule(st, mat, f)]
        if b.coupon_dates(st, mat, f) != [d for d in days if d > st]:
            wrong.append(("dates", mat, st, f))
        if b.previous_coupon(st, mat, f) != max(d for d in days if d <= st):
            wrong.append(
                ("previous", mat, st, f, b.previous_coupon(st, mat, f), max(d for d in days if d <= st))
            )
    oracle.check("bonds.coupon_dates, previous_coupon", "QuantLib Schedule (Backward, Unadjusted, no EOM)", grid=n,
                 max_diff=len(wrong), limit=0, why="mismatched schedules (dates are exact)", worst=wrong[:3])  # fmt: skip


def test_dirty_price_matches_quantlib_street_convention(oracle):
    """Both sides sum c / (1 + y/f)^(n + fraction) in double precision; the fraction of the first period is one
    division of two day counts on each side. Tolerance 1e-12 relative: a few units of float rounding over at most
    108 cash flows, nothing else."""
    worst, n = (0.0, None), 0
    for mat, st, c, f, y in cases():
        n += 1
        bond, rate = street(schedule(st, mat, f), f, c, y)
        ref = ql.CashFlows.npv(bond.cashflows(), rate, False, qd(st), qd(st))
        rel = abs(float(b.dirty_price(y, st, mat, c, f)) / ref - 1)
        worst = max(worst, (rel, (mat, st, c, f, y, ref)), key=lambda x: x[0])
    oracle.check("bonds.dirty_price", "QuantLib CashFlows.npv, ActualActual(ISMA), Compounded", grid=n,
                 max_diff=worst[0], limit=1e-12, why="relative (float rounding)", worst=worst[1])  # fmt: skip


def test_duration_and_convexity_match_quantlib(oracle):
    """The module rounds each figure to 6 decimals, so the tolerance is half a unit of the sixth decimal plus float
    rounding. QuantLib's convexity is (1/P) d2P/dy2 at the same compounding, the module's definition."""
    worst, n = {"macaulay": (0.0, None), "modified": (0.0, None), "convexity": (0.0, None)}, 0
    for mat, st, c, f, y in cases():
        n += 1
        bond, rate = street(schedule(st, mat, f), f, c, y)
        got = b.duration(y, st, mat, c, f)
        ref = {
            "macaulay": ql.BondFunctions.duration(bond, rate, ql.Duration.Macaulay, qd(st)),
            "modified": ql.BondFunctions.duration(bond, rate, ql.Duration.Modified, qd(st)),
            "convexity": ql.BondFunctions.convexity(bond, rate, qd(st)),
        }
        for k, v in ref.items():
            worst[k] = max(
                worst[k], (abs(float(getattr(got, k)) - v), (mat, st, c, f, y, v)), key=lambda x: x[0]
            )
    for k, (diff, ctx) in worst.items():
        oracle.check(f"bonds.duration.{k}", "QuantLib BondFunctions", grid=n, max_diff=diff, limit=5.0001e-7,
                     why="absolute (the module rounds to 6 decimals)", worst=ctx)  # fmt: skip


def test_ytm_matches_quantlib_bond_yield(oracle):
    """The yield of the module's own dirty price (its clean price plus its accrued interest), solved by QuantLib's
    root finder to 1e-13. The module bisects and quantises to 1e-8, so the tolerance is half of that plus the
    oracle's own accuracy; the yields are not round at 8 decimals, so the quantisation is really exercised."""
    worst, n = (0.0, None), 0
    for mat, st, c, f, y in cases(
        ["0", "0.09", "0.1425"], ["0.0213579135", "0.0735412345678", "0.2468013579"]
    ):
        if (
            st.toordinal() + mat.toordinal() + f + int(float(y) * 400)
        ) % 18:  # 1 in 18: Decimal bisection is slow
            continue
        n += 1
        clean = b.clean_price(y, st, mat, c, f)
        bond, rate = street(schedule(st, mat, f), f, c, y)
        dirty = float(clean + b.accrued_interest(st, mat, c, f))
        ref = ql.BondFunctions.bondYield(bond, ql.BondPrice(dirty, ql.BondPrice.Dirty), rate.dayCounter(),
                                         ql.Compounded, FREQ[f], qd(st), 1e-13, 1000, 0.05)  # fmt: skip
        got = b.ytm(clean, st, mat, c, f)
        worst = max(worst, (abs(float(got) - ref), (mat, st, c, f, y, got, ref)), key=lambda x: x[0])
        assert abs(got - Decimal(y)) <= Decimal("1e-8")  # and it returns the yield the price was built from
    oracle.check("bonds.ytm", "QuantLib BondFunctions.bondYield (dirty price)", grid=n, max_diff=worst[0],
                 limit=6e-9, why="absolute (the module quantises to 1e-8)", worst=worst[1])  # fmt: skip


def _accrual_refs(st: date, mat: date, f: int) -> tuple[ql.Date, ql.Date, ql.Date, ql.Date, ql.Date]:
    """(previous coupon, next coupon, start and end of the one-year period between two anniversaries of the
    maturity that holds the accrual), all from QuantLib's date arithmetic."""
    days = list(schedule(st, mat, f))
    prev = max(d for d in days if d <= qd(st))
    nxt = min(d for d in days if d > qd(st))
    years = list(schedule(st, mat, 1))
    start = max(d for d in years if d <= prev)
    end = min(d for d in years if d > prev)
    return prev, nxt, start, end


def test_accrued_interest_is_sebi_actual_actual_on_the_anniversary_year(oracle):
    """SEBI's rule in QuantLib's words: ActualActual(ISMA) with the ONE-YEAR period between two anniversaries of the
    maturity as the reference period, whatever the coupon frequency: days accrued / days in that year (365, or 366
    when it holds 29 February). The module adds coupon/basis day by day in Decimal; QuantLib divides two day counts
    in double precision, so the tolerance is float rounding (1e-12 relative). The grid settles on 29 February, the
    day before and the day after, and in anniversary years with and without one."""
    isma = ql.ActualActual(ql.ActualActual.ISMA)
    worst, n, leap = (0.0, None), 0, 0
    for mat, st, c, f, _ in cases(["0.0675", "0.09", "0.1425"], ["0"]):
        n += 1
        prev, _, start, end = _accrual_refs(st, mat, f)
        leap += end - start == 366
        ref = 100.0 * float(c) * isma.yearFraction(prev, qd(st), start, end)
        got = float(b.accrued_interest(st, mat, c, f))
        diff = abs(got - ref) / max(ref, 1e-300) if ref else abs(got)
        worst = max(worst, (diff, (mat, st, c, f, got, ref)), key=lambda x: x[0])
    assert leap > 400  # the leap-year branch is really on the grid
    oracle.check("bonds.accrued_interest", "QuantLib ActualActual(ISMA) on the anniversary year (SEBI's rule)",
                 grid=n, max_diff=worst[0], limit=1e-12, why="relative (Decimal vs float)", worst=worst[1])  # fmt: skip


def test_accrued_interest_gap_to_coupon_period_isma_is_the_stated_convention(oracle):
    """(b) convention. Bond desks outside India accrue ActualActual(ISMA) on the COUPON period: days / (freq x days
    in the coupon period), which reaches exactly one coupon at the period's end. SEBI's rule divides by the year, so
    the two differ by coupon x days x (1/basis - 1/(freq x period days)): every case is checked against that
    identity, and the largest gap is reported, not hidden."""
    worst, n, unexplained = (0.0, None), 0, 0.0
    for mat, st, c, f, _ in cases(["0.0675", "0.09", "0.1425"], ["0"]):
        n += 1
        sched = schedule(st, mat, f)
        bond = ql.FixedRateBond(0, 100.0, sched, [float(c)], ql.ActualActual(ql.ActualActual.ISMA, sched))
        ref = ql.BondFunctions.accruedAmount(bond, qd(st))
        prev, nxt, start, end = _accrual_refs(st, mat, f)
        d = qd(st) - prev
        explained = 100.0 * float(c) * d * (1 / (end - start) - 1 / (f * (nxt - prev)))
        gap = float(b.accrued_interest(st, mat, c, f)) - ref
        unexplained = max(unexplained, abs(gap - explained))
        worst = max(worst, (abs(gap), (mat, st, c, f, gap)), key=lambda x: x[0])
    assert unexplained < 1e-12
    oracle.note("bonds.accrued_interest", "QuantLib FixedRateBond accruedAmount, ActualActual(ISMA) on the coupon period",
                grid=n, max_diff=worst[0], tolerance="none: per 100 face; every gap equals c x days x (1/basis - 1/(f x L))",
                verdict="(b) convention: SEBI divides by the anniversary year, ISMA by the coupon period")  # fmt: skip


def test_a_full_periods_accrual_is_not_the_coupon_the_module_pays(oracle):
    """NEEDS DECISION (not a QuantLib difference: the module against itself, found while pinning the accrual).
    `cash_flows` pays face x coupon / freq every period, while `accrued_interest` accrues actual days over the
    year. For annual coupons the two meet (the period is the year). For half-yearly, quarterly and monthly coupons a
    full period accrues coupon x L / basis, not coupon / freq, so on the eve of a coupon the accrued interest is not
    the coupon about to be paid and the clean price of a par bond steps at the coupon date. If SEBI's denominator
    also sets the coupon AMOUNT (interest paid = face x rate x days / 365 or 366, which is how we read the circular's
    wording on the payment of interest; read on a secondary copy, not verified against SEBI's own PDF), day-count
    coupons would make the two consistent; an equal-coupon bond (a G-sec) would instead want ISMA accrual. Which
    instruments pay which way is the owner's call, and it moves every non-annual YTM a little, so it is measured
    here and left alone."""
    worst = {f: (0.0, None) for f in FREQ}
    step = {f: [math.inf, -math.inf] for f in FREQ}
    n = 0
    for mat in MATURITIES:
        for f in FREQ:
            for day in b.coupon_dates(date(2025, 1, 1), mat, f)[:-1]:
                n += 1
                eve = day - timedelta(days=1)
                prev, nxt, start, end = _accrual_refs(eve, mat, f)
                gap = 9.0 * (nxt - prev) / (end - start) - 9.0 / f  # per 100 face at a 9 % coupon
                worst[f] = max(worst[f], (abs(gap), (mat, day, gap)), key=lambda x: x[0])
                clean = float(b.clean_price("0.09", eve, mat, "0.09", f))
                step[f] = [min(step[f][0], clean), max(step[f][1], clean)]
    assert worst[1][0] < 1e-12  # annual: a full year's accrual is the coupon ...
    assert (
        99.998 < step[1][0] <= step[1][1] < 100.0
    )  # ... and par stays par, up to simple-vs-compound accrual
    # half-yearly: a 184-day half accrues 9 x 184/365 = 4.5370 against the 4.50 paid; monthly: February's 28 days
    # accrue 9 x 28/365 = 0.6904 against 0.75
    assert worst[2][0] == pytest.approx(9 * 184 / 365 - 4.5, abs=1e-12)
    assert worst[12][0] == pytest.approx(0.75 - 9 * 28 / 365, abs=1e-12)
    oracle.note("bonds.cash_flows vs bonds.accrued_interest", "the module itself (SEBI accrual, equal coupons)", grid=n,
                max_diff=max(w[0] for w in worst.values()),
                tolerance=(f"none: per 100 face at 9 %; a par bond's clean price on the eve of a coupon reads "
                           f"{step[2][0]:.3f} to {step[2][1]:.3f} (half-yearly), {step[12][0]:.3f} to {step[12][1]:.3f} (monthly)"),
                verdict="needs decision: day-count coupons (SEBI) or ISMA accrual for equal coupons")  # fmt: skip

"""fincalc/bonds.py against QuantLib's FixedRateBond, BondFunctions and CashFlows.

Conventions pinned on the QuantLib side to match the module's docstring before any difference counts:
* schedule: rolled BACK from the maturity date in steps of 12/freq months, each date taken from the maturity itself
  and clamped to the month's end, no business-day adjustment, end-of-month rule off, no settlement lag, face 100;
* coupons of a listed bond (#519, SEBI's Actual/Actual: NCS master circular of 15-Oct-2025, Chapter III;
  CIR/IMD/DF-1/122/2016, clause 3(b)): face x rate x actual days of the period / 365, or / 366 for every period of a
  coupon year that holds 29 February. In QuantLib's terms that is a `FixedRateCoupon` per period on
  `ActualActual(ISMA)` whose REFERENCE period is the one-year period between two anniversaries of the maturity:
  `sebi_bond` builds exactly that, and QuantLib computes every amount and accrual from it;
* coupons of a Government security (RBI's primer): half the annual coupon each half-year, accrued 30/360, which is
  QuantLib's `Thirty360(European)` (the primer names Excel's basis 4);
* discounting: `Compounded` at `freq`, time by `ActualActual(ISMA, schedule)` (the fraction of the current coupon
  period still to run, then whole periods): the street convention the module's "counts coupon periods" describes;
* accrued interest of a listed bond: NOT a ready-made QuantLib bond convention. The module follows SEBI's rule
  (CIR/IMD/DF-1/122/2016): when 29 February falls in a one-year period, 366 is the denominator for that whole year,
  whatever the coupon frequency. It is rebuilt here from QuantLib's own dates and day counts, and the gap to
  ActualActual(ISMA) on the coupon period is measured and explained.

The grid is built to hit what tests/test_bonds.py never did: settlement between coupon dates, on the eve and the day
of a coupon, on and around 29 February, month-end maturities (29th, 30th, 31st), and all four frequencies.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from decimal import Decimal

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


def sebi_bond(st: date, mat: date, freq: int, c: str) -> ql.Bond:
    """A listed bond in QuantLib's own objects: one `FixedRateCoupon` per period of the backward schedule, on
    `ActualActual(ISMA)` with the anniversary year that holds the period as its reference period, so QuantLib pays
    nominal x rate x days / days of that year. Nothing here divides days by a basis: the dates come from QuantLib's
    schedules and the amounts from its coupons. `ql.Bond` adds the redemption at the last coupon date."""
    days, years = list(schedule(st, mat, freq)), list(schedule(st, mat, 1))
    isma, leg = ql.ActualActual(ql.ActualActual.ISMA), []
    for start, end in itertools.pairwise(days):
        ref_start = max(d for d in years if d <= start)
        ref_end = min(d for d in years if d > start)
        leg.append(ql.FixedRateCoupon(end, 100.0, float(c), isma, start, end, ref_start, ref_end))
    return ql.Bond(0, ql.NullCalendar(), days[0], ql.Leg(leg))


def street(st: date, mat: date, freq: int, c: str, y: str):
    """The listed bond and the street yield convention: compounded `freq` times a year, time counted in coupon
    periods (`ActualActual(ISMA)` on the schedule, which ignores the coupons' own reference periods)."""
    dc = ql.ActualActual(ql.ActualActual.ISMA, schedule(st, mat, freq))
    return sebi_bond(st, mat, freq, c), ql.InterestRate(float(y), dc, ql.Compounded, FREQ[freq])


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
        bond, rate = street(st, mat, f, c, y)
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
        bond, rate = street(st, mat, f, c, y)
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
        bond, rate = street(st, mat, f, c, y)
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


def test_listed_bond_coupons_match_quantlib_coupons_on_the_anniversary_year(oracle):
    """#519. Every coupon the module pays against the amount of QuantLib's own coupon object (`sebi_bond`): nominal x
    rate x ActualActual(ISMA) year fraction with the anniversary year as reference period, which is SEBI's "366 days
    ... for the entire year, irrespective of whether the interest/ dividend is payable annually, half yearly,
    quarterly or monthly". The module divides in Decimal, QuantLib in double precision: 1e-12 relative. The grid
    must really hold unequal coupons (leap and non-leap years, 28- to 31-day months, clamped month ends)."""
    worst, n, unequal = (0.0, None), 0, 0
    for mat, st, f in sorted({(m, s, f) for m, s, _, f, _ in cases(["0"], ["0"])}):
        ref = [cf.amount() for cf in sebi_bond(st, mat, f, "0.09").cashflows()]
        got = b.cash_flows(st, mat, "0.09", f)
        assert len(ref) == len(got) + 1 and ref[-1] == 100.0  # QuantLib lists the redemption on its own
        for cf, want in zip(got, ref[:-1], strict=True):
            n += 1
            unequal += abs(want - 9.0 / f) > 1e-6
            worst = max(
                worst, (abs(float(cf.coupon) / want - 1), (mat, st, f, cf.day, want)), key=lambda x: x[0]
            )
            assert cf.amount == cf.coupon + (100 if cf.day == mat else 0)
    assert unequal > 10000  # the day-count branch is really on the grid (annual coupons are all 9.0)
    oracle.check("bonds.cash_flows (coupon amounts, SEBI)", "QuantLib FixedRateCoupon, ActualActual(ISMA) on the "
                 "anniversary year", grid=n, max_diff=worst[0], limit=1e-12, why="relative (Decimal vs float)",
                 worst=worst[1])  # fmt: skip


def test_a_full_periods_accrual_is_the_coupon_paid(oracle):
    """#519, the finding the first slice left under "needs decision", now closed: `cash_flows` paid face x coupon /
    freq while `accrued_interest` accrued actual days over the year, so a 184-day half-year accrued 4.537 per 100 at
    9 % and paid 4.50, and a par bond's clean price stepped at each coupon date (by up to 0.037 half-yearly, 0.055
    quarterly and 0.057 monthly per 100, measured on this grid with the module as it was). Both now come from one sum. Checked on the eve of every coupon of the grid, against QuantLib's coupon:
    its accrued amount on the eve, and its amount less that (one day's interest) against the module's. Then the step
    itself: across a coupon date the clean price of a 9 % bond at a 9 % yield may move only by one day's discounting
    of the dirty price less one day's accrual, which is far below one day's interest (9/365 per 100)."""
    worst, jump, n = (0.0, None), {f: 0.0 for f in FREQ}, 0
    for mat in MATURITIES:
        for f in FREQ:
            for day in b.coupon_dates(date(2025, 1, 1), mat, f)[:-1]:
                n += 1
                eve = day - timedelta(days=1)
                coupon = next(c for c in map(ql.as_coupon, sebi_bond(eve, mat, f, "0.09").cashflows())
                              if c is not None and c.date() == qd(day))  # fmt: skip
                paid = next(cf for cf in b.cash_flows(eve, mat, "0.09", f) if cf.day == day).coupon
                accrued = b.accrued_interest(eve, mat, "0.09", f)
                diffs = (abs(float(accrued) - coupon.accruedAmount(qd(eve))),
                         abs(float(paid - accrued) - (coupon.amount() - coupon.accruedAmount(qd(eve)))))  # fmt: skip
                worst = max(
                    worst, (max(diffs), (mat, day, f, float(paid), float(accrued))), key=lambda x: x[0]
                )
                step = float(
                    b.clean_price("0.09", day, mat, "0.09", f) - b.clean_price("0.09", eve, mat, "0.09", f)
                )
                jump[f] = max(jump[f], abs(step))
    oracle.check("bonds.accrued_interest on the eve of a coupon; bonds.cash_flows less it", "QuantLib FixedRateCoupon "
                 "accruedAmount and amount (anniversary year)", grid=n, max_diff=worst[0], limit=1e-12,
                 why="absolute per 100 face (Decimal vs float)", worst=worst[1])  # fmt: skip
    oracle.check("bonds.clean_price across a coupon date (cash_flows against accrued_interest)", "the module itself: "
                 "one day's discount less one day's accrual", grid=n, max_diff=max(jump.values()), limit=9 / 365,
                 why=(f"per 100 face at 9 %: below one day's interest; half-yearly {jump[2]:.4f}, monthly "
                      f"{jump[12]:.4f} (0.0373 and 0.0574 before #519)"), worst=jump)  # fmt: skip


def test_government_security_rule_matches_quantlib(oracle):
    """The other instrument class (#519), half-yearly only, as dated G-secs and SDLs pay. RBI's primer: "half yearly
    payment being half of the annual coupon", accrued on "30/360", for which its Annex 6 names Excel's basis 4: the
    European 30/360, QuantLib's `Thirty360(European)`.
    * accrued interest against QuantLib's accrued amount of a `FixedRateBond` on that day count, every settlement of
      the grid (31sts, 28 and 29 February): 1e-12, Decimal against float;
    * dirty price against the same street convention as the listed bond, on QuantLib's equal-coupon bond
      (`ActualActual(ISMA)` coupons are rate / 2 for every regular period): 1e-12 relative;
    * (b) convention, measured: had the COUPON been derived from the 30/360 count, a month-end security (28-Feb and
      31-Aug) would pay 178/360 and 182/360 of the annual coupon. RBI states half and half, so the module pays that
      and its 30/360 accrual does not land exactly on the coupon on such a security's coupon eve."""
    e30, worst_ai, worst_px, gap, n, n_px = (
        ql.Thirty360(ql.Thirty360.European),
        (0.0, None),
        (0.0, None),
        0.0,
        0,
        0,
    )
    for mat, st, c, f, y in cases(["0.0675", "0.09", "0.1425"]):
        if f != 2:
            continue
        sched = schedule(st, mat, 2)
        counted = ql.FixedRateBond(0, 100.0, sched, [float(c)], e30)
        equal = ql.FixedRateBond(0, 100.0, sched, [float(c)], ql.ActualActual(ql.ActualActual.ISMA, sched))
        got = b.cash_flows(st, mat, c, 2, day_count=b.GSEC)
        assert {cf.coupon for cf in got} == {100 * Decimal(c) / 2}
        gap = max(gap, *(abs(cf.amount() - 100 * float(c) / 2) for cf in list(counted.cashflows())[:-1]))
        if y == YIELDS[0]:  # the accrual does not depend on the yield
            n += 1
            diff = abs(
                float(b.accrued_interest(st, mat, c, 2, day_count=b.GSEC)) - counted.accruedAmount(qd(st))
            )
            worst_ai = max(worst_ai, (diff, (mat, st, c)), key=lambda x: x[0])
        n_px += 1
        rate = ql.InterestRate(
            float(y), ql.ActualActual(ql.ActualActual.ISMA, sched), ql.Compounded, ql.Semiannual
        )
        ref = ql.CashFlows.npv(equal.cashflows(), rate, False, qd(st), qd(st))
        rel = abs(float(b.dirty_price(y, st, mat, c, 2, day_count=b.GSEC)) / ref - 1)
        worst_px = max(worst_px, (rel, (mat, st, c, y, ref)), key=lambda x: x[0])
    oracle.check("bonds.accrued_interest (GSEC)", "QuantLib FixedRateBond accruedAmount, Thirty360(European)", grid=n,
                 max_diff=worst_ai[0], limit=1e-12, why="absolute per 100 face (Decimal vs float)",
                 worst=worst_ai[1])  # fmt: skip
    oracle.check("bonds.dirty_price (GSEC, equal coupons)", "QuantLib CashFlows.npv, ActualActual(ISMA) coupons, "
                 "Compounded", grid=n_px, max_diff=worst_px[0], limit=1e-12, why="relative (float rounding)",
                 worst=worst_px[1])  # fmt: skip
    oracle.note("bonds.cash_flows (GSEC coupon amounts)", "QuantLib FixedRateBond coupons counted on "
                "Thirty360(European)", grid=n_px, max_diff=gap,
                tolerance="none: per 100 face; 0 unless the coupon day is a clamped month end (178 or 182 of 360 days)",
                verdict="(b) convention: RBI pays half the annual coupon each half-year, whatever its 30/360 count")  # fmt: skip

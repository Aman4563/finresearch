"""fincalc/funds.xirr (and portfolio/analytics_math.investor_xirr, which calls it) against QuantLib.

Convention pinned first: the module discounts each flow by (1 + r)^(days / 365) with days counted from the earliest
flow. In QuantLib's words that is `Actual365Fixed`, `Compounded`, `Annual`, with the first flow's date as both the
settlement and the NPV date. Leap years therefore add a day, never change the divisor.

QuantLib is the oracle for the DISCOUNTING. For the root it is used twice: scipy's `brentq` on QuantLib's NPV
everywhere, and QuantLib's own solver (`CashFlows.yieldRate`) as a second witness. That solver searches for a bracket
by walking from its guess toward the smaller |NPV|; for a history of many instalments whose root lies below the guess
that is the wrong direction, so from the default guess of 5 % it fails on some, and is retried from lower guesses
(an oracle limitation, counted below).
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest
import QuantLib as ql
from scipy.optimize import brentq

from finresearch.fincalc import funds
from finresearch.portfolio import analytics_math as am

DC = ql.Actual365Fixed()
LO, HI = -0.9999, 10.0  # the module's stated bracket: -99.99 % to 1,000 %


def qd(d: date) -> ql.Date:
    return ql.Date(d.day, d.month, d.year)


def leg(flows):
    return [ql.SimpleCashFlow(float(a), qd(d)) for d, a in sorted(flows)], qd(min(d for d, _ in flows))


def ql_npv(flows, r: float) -> float:
    cf, d0 = leg(flows)
    return ql.CashFlows.npv(cf, ql.InterestRate(r, DC, ql.Compounded, ql.Annual), True, d0, d0)


def ql_root(flows) -> float:
    """The root of QuantLib's NPV inside the module's bracket (brentq, to about 1e-15)."""
    return brentq(lambda r: ql_npv(flows, r), LO, HI, xtol=1e-15, rtol=1e-15, maxiter=500)


def ql_own_root(flows) -> tuple[float | None, bool]:
    """QuantLib's own solver: (root or None, solved from the default guess)."""
    cf, d0 = leg(flows)
    for guess in (0.05, -0.5, -0.9, -0.99):
        try:
            root = ql.CashFlows.yieldRate(
                cf, 0.0, DC, ql.Compounded, ql.Annual, True, d0, d0, 1e-13, 500, guess
            )
        except RuntimeError:
            continue
        return root, guess == 0.05
    return None, False


def books(n: int, seed: int = 503):
    """Synthetic investor histories: 1 to 40 irregular purchases over up to 13 years (so several 29 Februaries),
    then one redemption worth 2 % to 12 times the money put in."""
    rng = random.Random(seed)
    for _ in range(n):
        d = date(2015 + rng.randrange(10), rng.randrange(1, 13), rng.randrange(1, 29))
        flows, paid = [], 0.0
        for _ in range(rng.randrange(1, 41)):
            amt = rng.choice([500, 1000, 2500, 10000, 123.45])
            flows.append((d, -amt))
            paid += amt
            d += timedelta(days=rng.randrange(1, 120))
        d += timedelta(days=rng.randrange(1, 900))
        flows.append((d, paid * rng.choice([0.02, 0.3, 0.7, 0.95, 1.0, 1.05, 1.5, 3.0, 12.0])))
        yield flows


def test_xirr_is_the_root_of_quantlibs_act365_annual_npv(oracle):
    """Tolerance 5e-9 absolute: the module rounds its root to 8 decimals (half a unit), brentq adds 1e-15. Where the
    module answers "no solution in (-99.99 %, 1000 %)" the QuantLib NPV must have no sign change in that bracket
    either: the root is outside it, which the module reports as unknown by design."""
    worst, n, outside, ql_own, ql_failed, ql_retried, ql_worst = (0.0, None), 0, 0, 0, 0, 0, 0.0
    for flows in books(3000):
        try:
            got = float(funds.xirr(flows))
        except ValueError:
            assert ql_npv(flows, LO) * ql_npv(flows, HI) > 0, flows
            outside += 1
            continue
        n += 1
        ref = ql_root(flows)
        worst = max(worst, (abs(got - ref), (got, ref, flows[0], flows[-1], len(flows))), key=lambda x: x[0])
        own, first_try = ql_own_root(flows)
        ql_retried += not first_try
        if own is None:
            ql_failed += 1
            continue
        ql_own += 1
        ql_worst = max(ql_worst, abs(got - own))
    assert n > 2500 and outside >= 10 and ql_own > 2500 and ql_retried > 300, (n, outside, ql_own, ql_retried)
    oracle.check("funds.xirr", "QuantLib NPV (Actual365Fixed, Compounded, Annual) + scipy brentq", grid=n,
                 max_diff=worst[0], limit=5.0001e-9, why="absolute (the module rounds to 8 decimals)", worst=worst[1])  # fmt: skip
    oracle.check("funds.xirr", "QuantLib CashFlows.yieldRate (its own solver)", grid=ql_own, max_diff=ql_worst,
                 limit=5.0001e-9, why="absolute (the module rounds to 8 decimals)")  # fmt: skip
    oracle.note("funds.xirr", "QuantLib CashFlows.yieldRate from its default guess of 5 %", grid=n, max_diff=ql_retried,
                tolerance=f"none: count of histories it could not bracket from 5 % ({ql_failed} still unsolved from "
                "-50 %, -90 % and -99 %)",
                verdict="(c) oracle limit: QuantLib's bracket search walks away from a root below its guess")  # fmt: skip
    oracle.note("funds.xirr", "QuantLib NPV at the bracket's ends", grid=outside, max_diff=outside,
                tolerance="none: count of roots outside (-99.99 %, 1000 %)",
                verdict="(b) convention: a root outside the bracket is unknown (ValueError), by design")  # fmt: skip


def test_xirr_counts_29_february_as_a_day_not_as_a_longer_year():
    """One rupee for exactly the leap year 2024 (366 days) at 10 % a year on Actual/365 is 1.10^(366/365); the
    module must return 10 % for it, and 10 % x 365/366-ish would mean it divided by the year's length."""
    end_value = 1000 * 1.10 ** (366 / 365)
    flows = [(date(2024, 1, 1), -1000), (date(2025, 1, 1), end_value)]
    assert float(funds.xirr(flows)) == pytest.approx(0.10, abs=5.0001e-9)
    assert ql_root(flows) == pytest.approx(0.10, abs=1e-12)


def test_investor_xirr_is_the_same_root_with_the_investors_signs(oracle):
    """investor_xirr takes net inflows as positive and the end value separately; the oracle sees the flipped flows."""
    worst, n = 0.0, 0
    for flows in books(300, seed=11):
        *buys, (end, value) = flows
        try:
            got = am.investor_xirr([(d, -a) for d, a in buys], end, value)
        except ValueError:
            continue
        n += 1
        worst = max(worst, abs(got - ql_root(flows)))
    oracle.check("analytics_math.investor_xirr", "QuantLib NPV + scipy brentq", grid=n, max_diff=worst,
                 limit=5.0001e-9, why="absolute (rounded to 8 decimals)")  # fmt: skip


def test_two_roots_inside_the_bracket_are_unknown_not_one_of_them(oracle):
    """(b) convention. -100, +230, -132 a year apart has two honest IRRs, 10 % and 20 %. The NPV has the same sign
    at both ends of the bracket, so the module raises (unknown) while QuantLib's solver returns whichever root its
    guess is nearer to. Unknown is the house rule for an ambiguous figure; the message says "no solution"."""
    flows = [(date(2023, 1, 1), -100), (date(2024, 1, 1), 230), (date(2024, 12, 31), -132)]
    assert abs(ql_npv(flows, 0.10)) < 1e-9 and abs(ql_npv(flows, 0.20)) < 1e-9
    with pytest.raises(ValueError, match="no solution"):
        funds.xirr(flows)
    cf, d0 = leg(flows)
    found = {round(ql.CashFlows.yieldRate(cf, 0.0, DC, ql.Compounded, ql.Annual, True, d0, d0, 1e-12, 1000, g), 6)
             for g in (0.05, 0.12, 0.18, 0.3)}  # fmt: skip
    assert found <= {0.1, 0.2} and found
    oracle.note("funds.xirr (two sign changes)", "QuantLib CashFlows.yieldRate", grid=1, max_diff=len(found),
                tolerance="none: QuantLib returns one of the two roots (10 %, 20 %), the module raises",
                verdict="(b) convention: an ambiguous IRR is unknown; the error text says 'no solution'")  # fmt: skip

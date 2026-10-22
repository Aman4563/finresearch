"""Event volatility (#391): implied event moves from the term structure (unknown when it cannot identify them) and
realised event moves that use only events known before each date (point in time)."""

import math
from datetime import date

import pytest

from finresearch.fincalc import eventvol as ev

# The issue's worked example (3-Jun-2024, NIFTY): near 13-Jun at 26.74 %, next 20-Jun at 22.58 %, calendar clock.
# Expected values from total variances w = sigma^2 T: base = (w2 - w1) / (T2 - T1), J^2 = w1 - base x T1.
NEAR = ev.Point(date(2024, 6, 13), 10 / 365, 0.2674)
NEXT = ev.Point(date(2024, 6, 20), 17 / 365, 0.2258)
EVENT = date(2024, 6, 4)


def test_two_expiries_after_the_event_give_the_issue_example():
    got = ev.two_after(NEAR, NEXT)
    assert got.j2 == pytest.approx(0.001365131272, rel=1e-9)
    assert math.sqrt(got.base_var) == pytest.approx(0.1472259100, rel=1e-9)  # the issue's ~14.7 % base vol
    assert got.move == pytest.approx(0.0369476829, rel=1e-9)  # the issue's ~3.7 % one-sigma move
    assert got.expected_abs == pytest.approx(math.sqrt(2 / math.pi) * 0.0369476829, rel=1e-9)
    assert got.median_abs == pytest.approx(0.6744897502 * 0.0369476829, rel=1e-9)
    assert got.reason is None


def test_inverted_term_structure_is_unknown_never_a_zero_move():
    got = ev.two_after(ev.Point(NEAR.expiry, NEAR.tau, 0.20), ev.Point(NEXT.expiry, NEXT.tau, 0.22))
    assert got.j2 is None and got.move is None and got.expected_abs is None
    assert "cannot be identified" in got.reason
    flat = ev.two_after(ev.Point(NEAR.expiry, NEAR.tau, 0.2), ev.Point(NEXT.expiry, NEXT.tau, 0.2))
    assert flat.j2 is None  # exactly flat: no event premium shown, and never "0 %"


def test_one_expiry_before_and_one_after_the_event():
    before = ev.Point(date(2024, 6, 3), 1 / 365, 0.20)
    got = ev.straddling(before, NEXT)
    assert got.j2 == pytest.approx(17 / 365 * (0.2258**2 - 0.04), rel=1e-12)
    assert got.base_var == pytest.approx(0.04)
    assert ev.straddling(ev.Point(date(2024, 6, 3), 1 / 365, 0.25), NEXT).j2 is None  # sigma2 < sigma1


def test_unusable_inputs_are_unknown():
    assert ev.two_after(NEAR, NEAR).j2 is None  # same expiry: no term structure
    assert ev.two_after(ev.Point(NEAR.expiry, NEAR.tau, None), NEXT).reason  # a missing IV
    assert ev.two_after(ev.Point(NEAR.expiry, 0.0, 0.2), NEXT).j2 is None  # expired

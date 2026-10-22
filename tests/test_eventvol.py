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


# Three expiries built from base 15 % and two events: A (J^2 = 0.001) before every expiry, B (J^2 = 0.0004) between
# the second and the third. w_j = 0.0225 tau_j + the events before T_j, so the fit must recover them exactly.
TAUS = (date(2024, 6, 13), 5 / 365), (date(2024, 6, 20), 12 / 365), (date(2024, 7, 25), 33 / 365)
EV_A, EV_B = date(2024, 6, 10), date(2024, 7, 1)


def _pts(j_a: float = 0.001, j_b: float = 0.0004) -> list:
    out = []
    for exp, tau in TAUS:
        w = 0.0225 * tau + j_a + (j_b if exp >= EV_B else 0.0)
        out.append(ev.Point(exp, tau, math.sqrt(w / tau)))
    return out


def test_fit_recovers_each_event_from_three_expiries():
    got = ev.fit(_pts(), [EV_A, EV_B])
    assert got[EV_A].j2 == pytest.approx(0.001, rel=1e-9)
    assert got[EV_B].j2 == pytest.approx(0.0004, rel=1e-9)
    assert got[EV_A].base_var == pytest.approx(0.0225, rel=1e-9)


def test_fit_on_two_expiries_matches_the_closed_form():
    assert ev.fit([NEAR, NEXT], [EVENT])[EVENT].j2 == pytest.approx(ev.two_after(NEAR, NEXT).j2, rel=1e-12)


def test_fit_leaves_an_event_with_no_premium_unknown_and_keeps_the_other():
    got = ev.fit(_pts(j_b=-0.0003), [EV_A, EV_B])  # the far expiry is priced below base: B shows no premium
    assert got[EV_B].j2 is None and "no premium" in got[EV_B].reason
    assert got[EV_A].j2 is not None and got[EV_A].j2 > 0


def test_fit_cannot_split_events_between_the_same_expiries_or_after_the_last():
    twin = date(2024, 7, 2)
    got = ev.fit(_pts(), [EV_A, EV_B, twin, date(2024, 8, 1)])
    assert got[EV_B].j2 is None and got[twin].j2 is None and "same expiries" in got[twin].reason
    assert got[date(2024, 8, 1)].j2 is None and "no expiry after" in got[date(2024, 8, 1)].reason
    assert got[EV_A].j2 is not None  # still identified from the three expiries


def test_fit_with_too_few_expiries_is_unknown():
    got = ev.fit(_pts()[1:], [EV_A, EV_B])  # 2 expiries, 3 unknowns (base, A, B)
    assert got[EV_A].j2 is None and got[EV_B].j2 is None and "too few" in got[EV_A].reason

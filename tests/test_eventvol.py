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


# ---------------------------------------------------------------- realised event moves, point in time
from datetime import datetime, timedelta  # noqa: E402

from finresearch.eventcal import calendar as cal  # noqa: E402
from finresearch.fincalc.dates import IST  # noqa: E402


def _ev(
    kind: str, day: date, known: date, *, withdrawn: date | None = None, at: datetime | None = None
) -> cal.Event:
    eod = lambda d: datetime(d.year, d.month, d.day, 23, 59, tzinfo=IST)  # noqa: E731
    return cal.Event(type=kind, tier=1, scope="IN", day=day, at=at, title=f"{kind} {day}",
                     source_url="https://example.gov/x", known_since=eod(known),
                     withdrawn_since=eod(withdrawn) if withdrawn else None)  # fmt: skip


# Synthetic closes: 100 on every weekday of June 2024, except the event days below.
CLOSES = {
    date(2024, 6, 1) + timedelta(days=i): 100.0
    for i in range(30)
    if (date(2024, 6, 1) + timedelta(days=i)).weekday() < 5
}
CLOSES[date(2024, 6, 4)] = 94.0  # results day (a Tuesday): -6 %
CLOSES[date(2024, 6, 5)] = 94.0
CLOSES.update({d: 94.0 for d in CLOSES if d > date(2024, 6, 4)})
CLOSES[date(2024, 6, 10)] = 95.0  # a Saturday event (8-Jun) lands on Monday's close


def test_realised_move_is_last_close_before_to_first_close_on_or_after_the_event():
    got = ev.realised_move(_ev("general_election", date(2024, 6, 4), date(2024, 3, 16)), CLOSES)
    assert got.r == pytest.approx(math.log(94 / 100), rel=1e-12)
    assert (got.start, got.end) == (date(2024, 6, 3), date(2024, 6, 4))
    sat = ev.realised_move(_ev("union_budget", date(2024, 6, 8), date(2024, 6, 1)), CLOSES)
    assert (sat.start, sat.end) == (date(2024, 6, 7), date(2024, 6, 10))
    assert sat.r == pytest.approx(math.log(95 / 94), rel=1e-12)


def test_realised_move_after_the_close_lands_on_the_next_session():
    late = datetime(2024, 6, 3, 23, 30, tzinfo=IST)  # after NSE's close: moves the 4th
    got = ev.realised_move(_ev("fomc", date(2024, 6, 3), date(2024, 1, 1), at=late), CLOSES)
    assert (got.start, got.end) == (date(2024, 6, 3), date(2024, 6, 4))


def test_realised_move_with_a_missing_close_is_unknown():
    gappy = {d: c for d, c in CLOSES.items() if not date(2024, 6, 3) <= d <= date(2024, 6, 12)}
    got = ev.realised_move(_ev("rbi_mpc", date(2024, 6, 7), date(2024, 3, 1)), gappy)
    assert got.r is None and "close" in got.reason


def test_history_uses_only_events_known_and_settled_before_as_of():
    events = [
        _ev("general_election", date(2024, 6, 4), date(2024, 3, 16)),
        _ev(
            "rbi_mpc", date(2024, 6, 7), date(2024, 6, 20)
        ),  # announced after the date (back-filled): unseen before
        _ev(
            "rbi_mpc", date(2024, 6, 6), date(2024, 3, 1), withdrawn=date(2024, 6, 2)
        ),  # moved: never happened
    ]
    on_4th = ev.history(events, CLOSES, datetime(2024, 6, 4, 12, 0, tzinfo=IST))
    assert on_4th == {}  # the 4th's close is not known at noon on the 4th
    after = ev.history(events, CLOSES, datetime(2024, 6, 4, 16, 0, tzinfo=IST))
    assert [m.day for m in after["general_election"].moves] == [date(2024, 6, 4)]
    assert "rbi_mpc" not in after
    later = ev.history(events, CLOSES, datetime(2024, 6, 25, 16, 0, tzinfo=IST))
    assert [m.day for m in later["rbi_mpc"].moves] == [date(2024, 6, 7)]  # the moved date never counts


def test_type_stats_median_and_mean_square():
    st = ev.TypeStats("rbi_mpc", [ev.Move(date(2024, 1, d), r, None, None, None) for d, r in
                                  [(1, 0.01), (2, -0.03), (3, 0.02)]])  # fmt: skip
    assert st.n == 3 and st.median_abs == pytest.approx(0.02) and st.mean_r2 == pytest.approx(0.0014 / 3)


# ---------------------------------------------------------------- flags
def test_elevated_names_the_event_and_strips_it_from_the_near_iv():
    imp = ev.two_after(NEAR, NEXT)
    got = ev.elevation(NEAR, [("Election results", imp)])
    assert got["status"] == "elevated" and "Election results" in got["note"]
    # ex-event IV: sqrt(sigma^2 - J^2 / tau) = sqrt(0.2674^2 - 0.001365131 x 36.5)
    assert got["ex_event_iv"] == pytest.approx(math.sqrt(0.2674**2 - 0.001365131272 * 36.5), rel=1e-8)
    assert got["event_share"] == pytest.approx(0.001365131272 / (0.2674**2 * 10 / 365), rel=1e-8)


def test_elevation_is_unknown_when_no_event_inside_could_be_measured():
    got = ev.elevation(NEAR, [("RBI policy", ev.Implied(None, reason="x"))])
    assert got["status"] == "unknown" and got["ex_event_iv"] is None
    assert ev.elevation(NEAR, [])["status"] == "none"


def _stats(n: int, size: float) -> ev.TypeStats:
    return ev.TypeStats("rbi_mpc", [ev.Move(date(2020, 1, 1) + timedelta(days=i), size * (-1) ** i, None, None,
                                            None) for i in range(n)])  # fmt: skip


def test_short_gamma_flag_needs_twenty_events_and_compares_median_moves():
    cheap = ev.Implied(0.005**2)  # implied median |move| 0.6745 x 0.5 % = 0.34 %, below the history's 1 %
    assert ev.short_gamma(cheap, _stats(20, 0.01))["status"] == "flag"
    assert ev.short_gamma(ev.Implied(0.03**2), _stats(20, 0.01))["status"] == "no_flag"  # 2.0 % > 1 %
    few = ev.short_gamma(cheap, _stats(19, 0.01))
    assert few["status"] == "too_few" and len(few["past_moves"]) == 19  # listed, never flagged
    assert ev.short_gamma(ev.Implied(None, reason="x"), _stats(25, 0.01))["status"] == "unknown"


def test_fairness_ratio_and_band_coverage_are_descriptive():
    pairs = [(0.02, 0.0004), (-0.01, 0.0004), (0.05, 0.0004)]  # z = 1, -0.5, 2.5
    got = ev.fairness(pairs)
    assert got["n"] == 3
    assert got["rho"] == pytest.approx((0.0004 + 0.0001 + 0.0025) / 3 / 0.0004)
    assert got["mean_z2"] == pytest.approx((1 + 0.25 + 6.25) / 3)
    assert got["within_1sd"] == pytest.approx(2 / 3)  # |z| <= 1 counts as inside
    assert got["within_2sd"] == pytest.approx(2 / 3)
    assert ev.fairness([])["rho"] is None

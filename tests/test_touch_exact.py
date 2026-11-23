"""#520: the exact risk-neutral touch probability (variant 2). Expected values are worked by hand from normal-table
values or computed here by a separate route (math.erfc), never read back from the function."""

from __future__ import annotations

import itertools
import math

import pytest

from finresearch.fincalc import black76
from finresearch.fincalc import risk_alerts as ra

F = 24000.0


def N(x: float) -> float:  # the normal CDF by erfc, not statistics.NormalDist (which the module uses)
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def exact(f: float, k: float, vol: float, t: float) -> float:
    """The first-passage probability written out from the Brownian-motion-with-drift form, with b = ln(K / F) and
    mu = -sigma^2 / 2, not from d1 and d2."""
    b, mu, s = math.log(k / f), -vol * vol / 2, vol * math.sqrt(t)
    if b < 0:
        return N((b - mu * t) / s) + math.exp(2 * mu * b / vol**2) * N((b + mu * t) / s)
    return N((-b + mu * t) / s) + math.exp(2 * mu * b / vol**2) * N((-b - mu * t) / s)


def test_hand_worked_values_on_both_sides_of_the_forward() -> None:
    # sigma sqrt(T) = 0.2, |ln(K / F)| = 0.1. Below: d1 = 0.6, d2 = 0.4; above: d1 = -0.4, d2 = -0.6.
    n04, n06 = 0.3445783, 0.2742531  # N(-0.4), N(-0.6) from the normal table
    below = n04 + 1.1051709 * n06  # N(-d2) + (F/K) N(-d1), F/K = e^0.1          = 0.6476749
    above = n06 + 0.9048374 * n04  # N(d2) + (F/K) N(d1),   F/K = e^-0.1         = 0.5860404
    assert ra.touch_prob_exact(100.0, 100.0 * math.exp(-0.1), 0.2, 1.0) == pytest.approx(below, abs=2e-7)
    assert ra.touch_prob_exact(100.0, 100.0 * math.exp(0.1), 0.2, 1.0) == pytest.approx(above, abs=2e-7)
    # the shortcut at the same strikes: 2 N(-0.4) = 0.68916 (too high for the put), 2 N(-0.6) = 0.54851 (too low)
    assert ra.touch_prob(100.0, 100.0 * math.exp(-0.1), 0.2, 1.0) == pytest.approx(2 * n04, abs=2e-7)
    assert ra.touch_prob(100.0, 100.0 * math.exp(0.1), 0.2, 1.0) == pytest.approx(2 * n06, abs=2e-7)
    assert 2 * n04 > below and 2 * n06 < above


@pytest.mark.parametrize(("m", "vol", "days"), list(itertools.product(
    [0.80, 0.95, 0.99, 0.999, 1.001, 1.01, 1.05, 1.20], [0.08, 0.15, 0.30, 0.60], [1, 7, 30, 90])))  # fmt: skip
def test_it_is_the_first_passage_law_of_a_brownian_motion_with_drift(m: float, vol: float, days: int) -> None:
    assert ra.touch_prob_exact(F, F * m, vol, days / 365) == pytest.approx(
        exact(F, F * m, vol, days / 365), abs=1e-12
    )


def test_limits_at_the_forward_and_far_from_it() -> None:
    assert ra.touch_prob_exact(F, F, 0.15, 30 / 365) == 1.0
    for eps in (1e-6, 1e-9):  # K -> F from either side
        assert ra.touch_prob_exact(F, F * (1 - eps), 0.15, 30 / 365) == pytest.approx(1.0, abs=1e-3)
        assert ra.touch_prob_exact(F, F * (1 + eps), 0.15, 30 / 365) == pytest.approx(1.0, abs=1e-3)
    assert ra.touch_prob_exact(F, F * 0.5, 0.15, 30 / 365) < 1e-12  # 16 sd away
    assert ra.touch_prob_exact(F, F * 2.0, 0.15, 30 / 365) < 1e-12
    grid = itertools.product([0.5, 0.9, 0.999, 1.001, 1.1, 2.0], [0.05, 0.3, 1.5], [1 / 365, 0.25, 3.0])
    assert all(0.0 <= ra.touch_prob_exact(F, F * m, v, t) <= 1.0 for m, v, t in grid)


def test_it_rises_with_volatility_and_with_time_on_both_sides() -> None:
    for m in (0.93, 1.07):
        by_vol = [ra.touch_prob_exact(F, F * m, v, 30 / 365) for v in (0.05, 0.10, 0.15, 0.25, 0.40, 0.80)]
        by_time = [ra.touch_prob_exact(F, F * m, 0.15, d / 365) for d in (1, 5, 15, 30, 90, 365)]
        assert by_vol == sorted(by_vol) and len(set(by_vol)) == len(by_vol)
        assert by_time == sorted(by_time) and len(set(by_time)) == len(by_time)
    # sigma and T enter only through sigma^2 T: 30 % for 30 days is 15 % for 120 days
    assert ra.touch_prob_exact(F, 22000.0, 0.30, 30 / 365) == pytest.approx(
        ra.touch_prob_exact(F, 22000.0, 0.15, 120 / 365), abs=1e-12
    )


def test_it_is_symmetric_in_log_moneyness_only_as_the_drift_vanishes() -> None:
    """At the same |ln(K / F)| = z sigma sqrt(T) the strike below is likelier to be touched (ln F drifts down at
    sigma^2 / 2); the two sides meet, at the driftless 2 N(-z), only as sigma^2 T -> 0."""
    z, gaps = 0.5, []
    for s in (0.4, 0.04, 0.004):  # sigma sqrt(T)
        below = ra.touch_prob_exact(100.0, 100.0 * math.exp(-z * s), s, 1.0)
        above = ra.touch_prob_exact(100.0, 100.0 * math.exp(z * s), s, 1.0)
        assert below > above
        gaps.append(below - above)
    assert gaps[0] > gaps[1] > gaps[2] > 0 and gaps[2] < 2e-3
    assert below == pytest.approx(2 * 0.3085375, abs=2e-3) and above == pytest.approx(2 * 0.3085375, abs=2e-3)


@pytest.mark.parametrize(("m", "vol", "days"), list(itertools.product(
    [0.85, 0.95, 0.98, 1.02, 1.05, 1.15], [0.12, 0.30, 0.60], [7, 30, 90])))  # fmt: skip
def test_it_is_the_shortcut_less_put_over_k_below_and_plus_call_over_k_above(
    m: float, vol: float, days: int
) -> None:
    """The identity the issue states: 2 N(-/+d2) is off by exactly the undiscounted option over the strike, with the
    side's sign (prices from fincalc.black76, discount factor 1), wherever the shortcut is not capped at 1."""
    k, t = F * m, days / 365
    shortcut = ra.touch_prob(F, k, vol, t)
    option = black76.price("put" if k < F else "call", F, k, t, 1.0, vol) / k
    assert option > 0
    if shortcut < 1.0:
        want = shortcut - option if k < F else shortcut + option
        assert ra.touch_prob_exact(F, k, vol, t) == pytest.approx(want, abs=1e-12)


def test_just_below_the_forward_the_shortcut_is_capped_at_one_and_the_exact_value_is_not() -> None:
    k, vol, t = F * 0.999, 0.30, 90 / 365  # ln(F / K) = 0.001 < sigma^2 T / 2 = 0.011, so 2 N(-d2) > 1
    assert ra.touch_prob(F, k, vol, t) == 1.0
    assert ra.touch_prob_exact(F, k, vol, t) == pytest.approx(exact(F, k, vol, t), abs=1e-12)
    assert ra.touch_prob_exact(F, k, vol, t) < 0.999


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_an_unknown_or_impossible_input_is_unknown_never_zero_or_one(bad: float | None) -> None:
    good = [F, 23000.0, 0.15, 30 / 365]
    for i in range(4):
        args = [*good]
        args[i] = bad
        assert ra.touch_prob_exact(*args) is None
    assert ra.touch_prob_exact(*good) == pytest.approx(
        exact(*good), abs=1e-12
    )  # the same call with all four known


def test_variant_1_is_unchanged_including_its_degenerate_answers() -> None:
    d2 = (math.log(F / 23000.0) - 0.15**2 * (30 / 365) / 2) / (0.15 * math.sqrt(30 / 365))
    assert ra.touch_prob(F, 23000.0, 0.15, 30 / 365) == pytest.approx(2 * N(-d2), abs=1e-12)
    assert (
        ra.touch_prob(F, 23000.0, 0.15, 0.0) == 1.0 and ra.touch_prob(F, 23000.0, 0.0, 0.1) == 1.0
    )  # as shipped


def test_the_continuity_correction_is_an_option_that_moves_the_barrier_away_and_is_off_by_default() -> None:
    # beta = -zeta(1/2) / sqrt(2 pi), zeta(1/2) = -1.4603545088095868 (Broadie, Glasserman and Kou 1997, Theorem 1.1)
    beta = ra.BGK_BETA
    assert beta == pytest.approx(1.4603545088095868 / math.sqrt(2 * math.pi), abs=1e-15)
    assert beta == pytest.approx(0.5826, abs=5e-5)
    vol, t, m = 0.15, 30 / 365, 21
    shift = math.exp(0.5826 * vol * math.sqrt(t / m))  # the paper's rounded beta
    for k, moved in (
        (F * 0.95, F * 0.95 / shift),
        (F * 1.05, F * 1.05 * shift),
    ):  # down for a put, up for a call
        got = ra.touch_prob_exact(F, k, vol, t, monitoring_dates=m)
        assert got == pytest.approx(exact(F, moved, vol, t), abs=1e-6)
        assert got < ra.touch_prob_exact(F, k, vol, t) == pytest.approx(exact(F, k, vol, t), abs=1e-12)
        many = ra.touch_prob_exact(
            F, k, vol, t, monitoring_dates=10**10
        )  # ever finer watching: the continuous value
        assert many == pytest.approx(exact(F, k, vol, t), abs=1e-5)
    assert ra.touch_prob_exact(F, F * 0.95, vol, t, monitoring_dates=0) is None
    assert ra.touch_prob_exact(F, F, vol, t, monitoring_dates=m) is None  # the barrier's side is not defined


def _strike_at(fn, vol: float, t: float, *, above: bool, level: float = ra.TOUCH_LEVEL) -> float:
    """The strike where fn crosses `level`, by bisection on the distance from the forward."""
    lo, hi = 1e-9, 2.0
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if fn(F, F * math.exp(mid if above else -mid), vol, t) > level else (lo, mid)
    return F * math.exp(lo if above else -lo)


def test_what_the_unchanged_0_60_level_means_under_each_variant() -> None:
    """15 % volatility, 30 days (ADDENDUM.md's table). Variant 1 fires where N(-/+d2) = 0.30 on both sides; variant 2
    fires on a put 0.05 % of F nearer and on a call 0.05 % of F further, at different terminal probabilities and
    deltas (undiscounted Black-76 forward delta: N(-d1) for a put, N(d1) for a call)."""
    vol, t = 0.15, 30 / 365
    s = vol * math.sqrt(t)
    want = {  # (variant, above the forward): (K / F - 1, N(-/+d2), delta)
        (ra.TOUCH_V1, False): (-0.023202, 0.3000, 0.2852), (ra.TOUCH_V1, True): (0.021862, 0.3000, 0.3151),
        (ra.TOUCH_V2, False): (-0.022711, 0.3041, 0.2892), (ra.TOUCH_V2, True): (0.022383, 0.2959, 0.3109),
    }  # fmt: skip
    for (key, above), (dist, itm, delta) in want.items():
        k = _strike_at(ra.TOUCH_VARIANTS[key][0], vol, t, above=above)
        d1 = (math.log(F / k) + s * s / 2) / s
        sign = 1 if above else -1
        assert k / F - 1 == pytest.approx(dist, abs=2e-6)
        assert N(sign * (d1 - s)) == pytest.approx(itm, abs=5e-5) and N(sign * d1) == pytest.approx(
            delta, abs=5e-5
        )

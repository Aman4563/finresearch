"""P4.5 (#398): the risk alerts' pure maths (fincalc.risk_alerts). Expected values are computed in each test from
the formula written out by hand (statistics.stdev, math.log, NormalDist), not by the module."""

from __future__ import annotations

import itertools
import math
from statistics import NormalDist, stdev

import pytest

from finresearch.fincalc import risk_alerts as ra


def _iv_series(n: int, bump: float = 0.0) -> list[float]:
    """A deterministic IV30 series: alternating ±0.5 vol-point changes around 15, then `bump` on the last session."""
    ivs = [15.0]
    for i in range(n - 1):
        ivs.append(ivs[-1] + (0.5 if i % 2 == 0 else -0.5))
    ivs[-1] = ivs[-2] + bump if bump else ivs[-1]
    return ivs


def test_iv_shock_z_is_todays_change_over_the_prior_252_changes_sd() -> None:
    ivs = _iv_series(300, bump=2.0)
    changes = [b - a for a, b in itertools.pairwise(ivs)]
    want = changes[-1] / stdev(changes[-253:-1])  # today's change excluded from its own sd
    got = ra.iv_shock(ivs)
    assert got.value == pytest.approx(want) and got.value >= ra.SHOCK_Z
    assert ra.iv_shock(_iv_series(300, bump=0.6)).value < ra.SHOCK_Z


def test_iv_shock_is_unknown_with_too_short_a_history_or_a_gap() -> None:
    short = ra.iv_shock(_iv_series(200, bump=3.0))
    assert short.value is None and "252" in short.detail
    gapped = _iv_series(300, bump=3.0)
    gapped[-20] = None  # type: ignore[call-overload]
    assert ra.iv_shock(gapped).value is None


def test_cumulative_shock_compares_the_5_session_change_with_past_5_session_changes() -> None:
    ivs = _iv_series(300)
    for k in range(1, 6):  # a slow climb: +1 vol point a session for 5 sessions, no single day a shock
        ivs[-6 + k] = ivs[-6] + k
    c = [ivs[i] - ivs[i - 5] for i in range(5, len(ivs))]
    # the 252 five-session changes ending at or before t-5 (none overlaps today's window): c[-6] ends at t-5
    past = c[-257:-5]
    assert len(past) == 252
    want = c[-1] / stdev(past)
    got = ra.cum_shock(ivs)
    assert got.value == pytest.approx(want) and got.value >= ra.SHOCK_Z
    assert ra.iv_shock(ivs).value < ra.SHOCK_Z  # the one-day rule alone would miss it


def test_gap_z_is_the_abs_log_gap_over_daily_sigma_of_close_to_close_returns() -> None:
    closes = [100 * (1.01 if i % 2 else 0.99) ** (i % 3) for i in range(260)]
    rets = [math.log(b / a) for a, b in itertools.pairwise(closes)]
    sigma = stdev(rets[-252:])
    got = ra.gap_z(open_=closes[-1] * 0.95, prev_close=closes[-1], closes=closes)
    assert got.value == pytest.approx(abs(math.log(0.95)) / sigma)
    assert ra.gap_z(open_=None, prev_close=closes[-1], closes=closes).value is None


def test_gap_tail_is_the_share_of_stored_gaps_beyond_the_distance() -> None:
    gaps = [0.001 * (i % 30 - 15) for i in range(300)]  # -1.5 % .. +1.4 %
    want = sum(abs(g) > 0.01 for g in gaps) / len(gaps)
    p, n = ra.gap_tail(gaps, 0.01)
    assert p == pytest.approx(want) and n == 300
    assert ra.gap_tail(gaps[:50], 0.01)[0] is None  # fewer than MIN_GAPS: unknown


def test_touch_probability_is_twice_the_risk_neutral_terminal_probability_capped_at_one() -> None:
    f, k, vol, t = 24000.0, 23000.0, 0.15, 10 / 365
    d2 = (math.log(f / k) - vol * vol * t / 2) / (vol * math.sqrt(t))
    assert ra.touch_prob(f, k, vol, t) == pytest.approx(2 * NormalDist().cdf(-d2))  # a put strike below F
    kc = 25000.0
    d2c = (math.log(f / kc) - vol * vol * t / 2) / (vol * math.sqrt(t))
    assert ra.touch_prob(f, kc, vol, t) == pytest.approx(2 * NormalDist().cdf(d2c))  # a call strike above F
    assert ra.touch_prob(f, f, vol, t) == 1.0
    assert "risk-neutral" in ra.TOUCH_LABEL


@pytest.mark.parametrize(("prev", "u", "want"), [
    ("clear", 0.79, "clear"), ("clear", 0.80, "fired"), ("fired", 0.75, "fired"), ("fired", 0.70, "fired"),
    ("fired", 0.69, "clear"), ("new", 0.9, "fired"), ("unknown", 0.75, "clear"),
])  # fmt: skip
def test_utilisation_fires_at_0_8_and_clears_only_below_0_7(prev: str, u: float, want: str) -> None:
    assert ra.hysteresis(prev, u) == want


def test_precision_and_mute_suggestions_never_touch_limit_rules() -> None:
    assert ra.precision(1, 10) == pytest.approx(0.1) and ra.precision(0, 0) is None
    assert ra.suggest_mute("shock", fired=10, actioned=1) is True
    assert ra.suggest_mute("shock", fired=9, actioned=0) is False  # too few firings to judge
    assert ra.suggest_mute("shock", fired=10, actioned=2) is False  # 20 % is not below 20 %
    for cat in ("limit", "margin", "es", "gap_stop"):
        assert ra.suggest_mute(cat, fired=50, actioned=0) is False

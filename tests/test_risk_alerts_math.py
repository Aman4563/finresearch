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
    got = ra.gap_z(open_=closes[-1] * 0.95, prev_close=closes[-1], rets=rets)
    assert got.value == pytest.approx(abs(math.log(0.95)) / sigma)
    assert ra.gap_z(open_=None, prev_close=closes[-1], rets=rets).value is None
    assert ra.gap_z(open_=95.0, prev_close=100.0, rets=rets[:100]).value is None  # short history: unknown


def test_front_future_series_pairs_each_session_with_the_same_contract_across_a_roll() -> None:
    from datetime import date

    e1, e2 = date(2026, 1, 27), date(2026, 2, 24)
    rows = [  # (day, expiry, open, close): the near contract expires on 27-Jan, then February is the front
        (date(2026, 1, 23), e1, 100.0, 101.0), (date(2026, 1, 23), e2, 101.5, 102.0),
        (date(2026, 1, 27), e1, 99.0, 100.0), (date(2026, 1, 27), e2, 100.0, 103.0),
        (date(2026, 1, 28), e2, 98.0, 99.0),
    ]  # fmt: skip
    got = ra.front_future(rows)
    assert [x.day for x in got] == [date(2026, 1, 27), date(2026, 1, 28)]
    jan27, jan28 = got
    assert jan27.gap == pytest.approx(math.log(99.0 / 101.0)) and jan27.ret == pytest.approx(
        math.log(100 / 101)
    )
    # 28-Jan: February is the front; its own previous close (103), never January's
    assert jan28.gap == pytest.approx(math.log(98.0 / 103.0)) and jan28.ret == pytest.approx(
        math.log(99 / 103)
    )
    assert jan28.open == 98.0 and jan28.prev_close == 103.0


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


def _quiet(n: int, start=None):
    """n weekday sessions: IV30 alternating 15.0/15.5, the front future alternating 24000/24240 (opens = closes)."""
    from datetime import date, timedelta

    d, days = start or date(2025, 1, 1), []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    ivs = [(x, 15.0 + (0.5 if i % 2 else 0.0)) for i, x in enumerate(days)]
    fut = [(x, date(2030, 1, 31), 24000 * (1.01 if i % 2 else 1.0), 24000 * (1.01 if i % 2 else 1.0))
           for i, x in enumerate(days)]  # fmt: skip
    return days, ivs, fut


def test_replay_fires_on_a_shock_day_and_scores_the_synthetic_books_follow_through() -> None:
    days, ivs, fut = _quiet(300)
    k = 280  # an even session: close 24000; the shock: IV +6 points and a 4 % gap down that keeps falling
    ivs[k] = (days[k], ivs[k - 1][1] + 6)
    for j in range(k, 300):
        ivs[j] = (days[j], ivs[k][1] + (0.5 if j % 2 else 0.0)) if j > k else ivs[k]
        lvl = 24000 * 0.96 * (0.97 ** (j - k))
        fut[j] = (days[j], fut[j][1], lvl, lvl)
    got = ra.replay(ivs, ra.front_future(fut))
    hit = next(f for f in got if f.day == days[k])
    assert {"iv_shock", "gap"} <= set(hit.rules)
    # the short 30-day ATM straddle struck at the shock day's close, repriced two sessions later
    from finresearch.fincalc.book_scenarios import b76_price

    f0, f2 = 24000 * 0.96, 24000 * 0.96 * 0.97**2
    v0, v2 = ivs[k][1] / 100, ivs[k + 2][1] / 100
    p0 = sum(b76_price(r, f0, f0, 30 / 365, 0.065, v0) for r in ("call", "put"))
    t2 = (30 - (days[k + 2] - days[k]).days) / 365
    p2 = sum(b76_price(r, f2, f0, t2, 0.065, v2) for r in ("call", "put"))
    assert hit.loss == pytest.approx((p2 - p0) / f0) and hit.false_alarm is False
    summ = ra.replay_summary(got, scenario_days=(days[k],), episodes=())
    assert summ["scenarios"][days[k].isoformat()] is True
    assert summ["years"][str(days[k].year)]["firings"] >= 1


def test_a_shock_with_no_follow_through_is_a_false_alarm_and_too_little_data_is_unknown() -> None:
    days, ivs, fut = _quiet(300)
    k = 281
    ivs[k] = (days[k], ivs[k - 1][1] + 6)  # one-day IV blip, reverted the next session, no price move
    got = ra.replay(ivs, ra.front_future(fut))
    hit = next(f for f in got if f.day == days[k])
    assert set(hit.rules) == {"iv_shock", "cum_shock"} and hit.false_alarm is True  # no gap: no price move
    days, ivs, fut = _quiet(300)
    ivs[299] = (days[299], ivs[298][1] + 6)  # the last session: no two sessions after it
    last = next(f for f in ra.replay(ivs, ra.front_future(fut)) if f.day == days[299])
    assert last.loss is None and last.false_alarm is None

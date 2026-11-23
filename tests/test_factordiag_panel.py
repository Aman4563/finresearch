"""The panel of the stock factor diagnostics (#506) on generated price series: scores use only closes up to the
decision date, forward returns only closes from it on, membership is taken as of the decision date (no survivorship,
no look-ahead), a stock that stops trading is held in cash and counted, a gap or a jump is unknown and counted, and a
deliberate look-ahead (a score peeking one bar ahead, or a forward return that starts one day early) is caught."""

from datetime import date, timedelta
from pathlib import Path

import factordiag_synth as fs
import numpy as np
import pytest

from finresearch.evals import stock_backtest as bt
from finresearch.factordiag import panel as pn
from finresearch.factordiag import specs, study
from finresearch.signals import stock as live

ROOT = Path(__file__).resolve().parents[1]
DAYS = fs.weekdays(date(2015, 1, 1), 800)  # about three years of generated sessions


# --------------------------------------------------------------------------------------------- scores at t
def test_name_inputs_match_an_independent_computation() -> None:
    closes = fs.walk(np.random.default_rng(1), 800, drift=0.0004)
    s = fs.series("SYN00", DAYS, closes)
    i = 700
    x = pn.name_inputs(s, i)
    c = np.array(closes[: i + 1])  # nothing after bar i
    daily = c[-252:] / c[-253:-1] - 1
    vol = float(daily.std(ddof=1) * np.sqrt(252))
    mom = float(c[-22] / c[-253] - 1)  # 12-1: from 252 sessions ago to 21 sessions ago
    assert x["vol"] == pytest.approx(vol) and x["mom_score"] == pytest.approx(mom / vol)
    assert x["trend_distance"] == pytest.approx(float(c[-1] / c[-200:].mean() - 1))
    assert pn.name_inputs(s, 251) is None  # 252 bars: one short of the harness's warm-up of 253
    assert pn.name_inputs(s, 252) is not None and bt.WARMUP == 253


def test_a_name_with_an_unexplained_jump_in_its_warm_up_is_not_eligible() -> None:
    closes = fs.walk(np.random.default_rng(2), 800)
    s = fs.series("SYN00", DAYS, closes, anomalies=[DAYS[500]])
    assert pn.name_inputs(s, 600) is None and pn.name_inputs(s, 499) is not None
    assert pn.name_inputs(s, 500 + 253) is not None  # the jump has left the 253-bar window


def test_component_scores_by_hand() -> None:
    inputs = {"A": {"mom_score": 2.0, "vol": 0.40, "trend_distance": 0.25},
              "B": {"mom_score": 0.75, "vol": 0.20, "trend_distance": 0.05},
              "C": {"mom_score": -0.3, "vol": 0.20, "trend_distance": -0.02},
              "D": {"mom_score": 0.75, "vol": 0.30, "trend_distance": -0.20}}  # fmt: skip
    sc = pn.component_scores(inputs)
    assert set(sc) == set(specs.COMPONENTS)
    assert sc["low_vol"] == {"A": -0.40, "B": -0.20, "C": -0.20, "D": -0.30}
    # momentum ranks (1 = highest): A 1, B and D tie at 2.5, C 4; volatility ranks (1 = lowest): B and C 1.5, D 3, A 4
    assert sc["mom_lowvol_rank"] == {"A": -2.5, "B": -2.0, "C": -2.75, "D": -2.75}
    # 40 x clip(mom / 1.5) + 20 x clip(trend / 10 %): A 40 + 20; B 20 + 10; C -8 - 4; D 20 - 20
    assert sc["composite_price"] == pytest.approx({"A": 60.0, "B": 30.0, "C": -12.0, "D": 0.0})
    assert pn.component_scores({}) == {c: {} for c in specs.COMPONENTS}


@pytest.mark.parametrize(
    ("mom", "trend"), [(0.9, 0.04), (2.4, 0.31), (-0.6, -0.03), (-3.0, -0.5), (0.0, 0.0)]
)
def test_composite_price_is_the_price_part_of_the_live_composite(mom: float, trend: float) -> None:
    """The registered weights restate inline literals of signals/stock.composite: this keeps the two equal."""
    f = live.Features(mom_score=mom, trend_distance=trend, mom_12_1=mom * 0.3, vol=0.3)
    factors = {x.name: x.contribution for x in live.composite(f, [])}
    price_part = factors["Momentum (12-1 month, risk-adjusted)"] + factors["Trend (price vs 200-day average)"]
    ours = pn.component_scores({"X": {"mom_score": mom, "vol": 0.3, "trend_distance": trend}})[
        "composite_price"
    ]["X"]
    assert ours == pytest.approx(price_part, abs=0.011)  # the live factors are rounded to 2 decimals each


# --------------------------------------------------------------------------------------------- forward returns
def _bars(*pairs: tuple[date, float], anomalies=()) -> bt.Series:
    return fs.series("SYN00", [d for d, _ in pairs], [c for _, c in pairs], anomalies)


T, T1, T2, T3 = date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 28), date(2024, 4, 30)


def test_forward_return_by_hand() -> None:
    s = _bars((T, 100.0), (date(2024, 2, 26), 110.0), (T2, 121.0), (T3, 90.0))
    assert pn.forward_return(s, T, T, T1) == (
        pytest.approx(0.10),
        None,
    )  # no bar on 29-Feb: the one 3 days before
    assert pn.forward_return(s, T, T, T2) == (pytest.approx(0.21), None)
    assert pn.forward_return(s, T, T1, T2) == (pytest.approx(0.10), None)  # the segment (1, 2]: 121 / 110 - 1
    assert pn.forward_return(s, T, T2, T3)[0] == pytest.approx(90 / 121 - 1)


def test_a_stock_that_stopped_trading_is_held_in_cash_from_its_last_close_and_flagged() -> None:
    s = _bars((T, 100.0), (date(2024, 2, 15), 120.0))  # the last bar is 14 days before the February month-end
    assert pn.forward_return(s, T, T, T1) == (pytest.approx(0.20), "ended")
    assert pn.forward_return(s, T, T, T3) == (pytest.approx(0.20), "ended")  # cash at 0 % from then on
    assert pn.forward_return(s, T, T1, T2) == (0.0, "ended")  # a later segment: already in cash
    only_t = _bars((date(2024, 1, 2), 90.0), (T, 100.0))
    assert pn.forward_return(only_t, T, T, T1) == (0.0, "ended")  # it stopped on the decision date itself


def test_a_gap_or_an_unexplained_jump_makes_the_return_unknown_never_zero() -> None:
    gap = _bars((T, 100.0), (date(2024, 2, 15), 120.0), (date(2024, 3, 20), 130.0), (T2, 131.0))
    assert pn.forward_return(gap, T, T, T1) == (
        None,
        "gap",
    )  # nothing within 7 days before 29-Feb, but it resumes
    assert pn.forward_return(gap, T, T, T2) == (pytest.approx(0.31), None)
    jump = _bars(
        (T, 100.0), (date(2024, 2, 20), 160.0), (T1, 161.0), (T2, 150.0), anomalies=[date(2024, 2, 20)]
    )
    assert pn.forward_return(jump, T, T, T1) == (None, "jump")
    assert pn.forward_return(jump, T, T, T2) == (None, "jump")  # every window that spans the jump
    assert pn.forward_return(jump, T, T1, T2)[1] is None  # a segment that starts after it is known
    before = _bars((date(2024, 1, 30), 60.0), (T, 100.0), (T1, 110.0), anomalies=[T])
    assert pn.forward_return(before, T, T, T1) == (pytest.approx(0.10), None)  # a jump ON t is not after t


# --------------------------------------------------------------------------------------------- membership as of t
LONG = fs.weekdays(
    date(2014, 1, 1), 1150
)  # about 4.4 years, so the first eligible month-end is about a year in


def _world(seed: int = 3, n: int = 26) -> tuple[dict[str, bt.Series], bt.Series]:
    rng = np.random.default_rng(seed)
    return fs.universe(rng, LONG, fs.names(n)), fs.series(fs.MARKET, LONG, fs.walk(rng, len(LONG), vol=0.008))


def test_membership_is_taken_as_of_the_decision_date() -> None:
    """SYN00 leaves the index and SYN25 joins it on 1-Jul-2017. A decision date before that keeps SYN00 in its
    cross-section for every horizon (its returns after the exclusion included) and has no SYN25, although SYN25 has
    prices all along: the later membership is not known at t."""
    uni, market = _world()
    change = date(2017, 7, 1)
    members = lambda t: fs.names(25) if t < change else fs.names(26)[1:]  # noqa: E731
    panel = pn.build(uni, market, members)
    by_date = {m.t: m for m in panel.months}
    before = by_date[
        max(t for t in by_date if t < date(2017, 1, 1))
    ]  # December 2016: 12 months reach past the change
    assert "SYN00" in before.scores["mom_score"] and "SYN25" not in before.scores["mom_score"]
    for window in pn.WINDOWS:
        assert "SYN00" in before.returns[window] and "SYN25" not in before.returns[window], window
    after = by_date[min(t for t in by_date if t >= change)]
    assert "SYN25" in after.scores["mom_score"] and "SYN00" not in after.scores["mom_score"]
    assert before.members == 25 and before.eligible == 25 and after.members == 25


def test_the_real_change_log_is_applied_only_up_to_the_decision_date() -> None:
    """The two pinned files of the base experiment: the first row of the log excludes JPASSOCIAT and includes TECHM
    with effect from 28-Mar-2014 (NSE Indices press release of 27-Feb-2014, cited in the log)."""
    members = study.load_membership(ROOT)
    day_before, effective = members(date(2014, 3, 27)), members(date(2014, 3, 28))
    assert "JPASSOCIAT" in day_before and "TECHM" not in day_before
    assert "TECHM" in effective and "JPASSOCIAT" not in effective
    assert len(day_before) == len(effective) == 50 and len(members(date(2026, 9, 30))) == 50


def test_windows_beyond_the_calendar_are_not_evaluated_and_early_months_have_no_names() -> None:
    uni, market = _world()
    panel = pn.build(uni, market, lambda t: fs.names(26))
    ends = bt.month_ends(market.adj.days)
    assert [m.t for m in panel.months] == ends
    assert set(panel.months[-1].returns) == set() and set(panel.months[-2].returns) == {(0, 1)}
    assert set(panel.months[-13].returns) == set(pn.WINDOWS)  # twelve month-ends remain after it
    assert (
        panel.months[0].eligible == 0 and panel.months[0].scores["mom_score"] == {}
    )  # under 253 bars: no score
    first = next(m for m in panel.months if m.eligible)
    assert first.t == next(t for t in ends if market.index_of[t] + 1 >= bt.WARMUP)


def test_ended_gapped_and_jumped_names_are_counted_and_only_the_ended_one_keeps_a_return() -> None:
    uni, market = _world()
    ends = bt.month_ends(market.adj.days)
    t, t1 = ends[-8], ends[-7]
    stop = t + timedelta(days=10)  # SYN01's last session: it stops trading (a delisting or a merger)
    hole = (t1 - timedelta(days=12), t1 + timedelta(days=5))  # SYN02 is suspended around the next month-end
    jump = t + timedelta(days=5)  # SYN03 has an unexplained jump just after t

    def cut(s: bt.Series, keep) -> bt.Series:
        pairs = [(d, c) for d, c in zip(s.adj.days, s.adj.close, strict=True) if keep(d)]
        return fs.series(s.symbol, [d for d, _ in pairs], [c for _, c in pairs])

    uni["SYN01"] = cut(uni["SYN01"], lambda d: d <= stop)
    uni["SYN02"] = cut(uni["SYN02"], lambda d: not hole[0] <= d <= hole[1])
    uni["SYN03"] = fs.series("SYN03", LONG, uni["SYN03"].adj.close, anomalies=[jump])
    panel = pn.build(uni, market, lambda _: fs.names(26))
    m = next(x for x in panel.months if x.t == t)
    one, three = m.returns[0, 1], m.returns[0, 3]
    assert "SYN01" in one and "SYN02" not in one and "SYN03" not in one
    assert m.flags[0, 1] == {"ended": 1, "gap": 1, "jump": 1}
    s1 = uni["SYN01"]
    assert one["SYN01"] == pytest.approx(s1.adj.close[-1] / s1.adj.close[s1.index_of[t]] - 1)
    assert three["SYN01"] == one["SYN01"]  # in cash at 0 % from the last close: the same return at 3 months
    assert (
        "SYN02" in three and "SYN03" not in three
    )  # the suspension is over; the jump is still inside the window
    assert m.flags[0, 3] == {"ended": 1, "jump": 1}
    assert set(panel.ended) == {"SYN01"} and panel.ended["SYN01"] >= 1 and panel.missing == {}


# --------------------------------------------------------------------------------------------- the look-ahead audit
def _members(_: date) -> list[str]:
    return fs.names(26)


def test_the_audit_passes_on_the_registered_panel_and_counts_its_checks() -> None:
    uni, market = _world()
    panel = pn.build(uni, market, _members)
    out = pn.audit(uni, market, _members, panel)
    ends = bt.month_ends(market.adj.days)
    picks = sorted({*range(0, len(ends), specs.AUDIT_EVERY), len(ends) - 1})
    assert out["passed"] is True and out["dates"] == len(picks)
    assert out["score_checks"] == sum(5 * panel.months[k].eligible for k in picks)
    assert (
        out["return_checks"]
        == sum(panel.months[k].eligible * len(panel.months[k].returns) for k in picks)
        > 0
    )


def test_a_score_that_peeks_one_bar_ahead_is_caught() -> None:
    uni, market = _world()

    def peeking(s: bt.Series, i: int):
        x = pn.name_inputs(s, i)
        if x is not None and i + 1 < len(s.adj.close):
            x["trend_distance"] = (
                s.adj.close[i + 1] / s.adj.close[i] - 1
            )  # tomorrow's return, known only tomorrow
        return x

    panel = pn.build(uni, market, _members, inputs_fn=peeking)
    with pytest.raises(pn.LookAheadError, match="after the decision date"):
        pn.audit(uni, market, _members, panel, inputs_fn=peeking)


def test_a_forward_return_leaking_one_day_is_caught() -> None:
    """The deliberate look-ahead of the brief: the 'forward' return starts at the close one session BEFORE the
    decision date, so it contains the decision day's own move, which every score has already seen."""
    uni, market = _world()

    def leaking(s: bt.Series, t: date, ta: date, tb: date):
        r, flag = pn.forward_return(s, t, ta, tb)
        i = s.index_of[t]
        if r is None or ta != t or i == 0:
            return r, flag
        return (1 + r) * s.adj.close[i] / s.adj.close[i - 1] - 1, flag  # from yesterday's close

    panel = pn.build(uni, market, _members, return_fn=leaking)
    with pytest.raises(pn.LookAheadError, match="closes before the decision date"):
        pn.audit(uni, market, _members, panel, return_fn=leaking)


def test_the_one_day_leak_shows_up_as_a_spurious_ic_which_is_why_the_audit_exists() -> None:
    """On pure-noise walks a score made of the decision day's own return has no skill against the true forward
    return, and a large 'IC' against the leaking one: the mean rank IC of that day's return with a one-month return
    that includes the day is about sqrt(1 / 22) = 0.21 (one of about 22 equally noisy sessions), far outside the
    correct series' interval."""
    uni, market = _world(seed=9)
    ends = [t for t in bt.month_ends(market.adj.days) if market.index_of[t] >= 1][:-1]
    nxt = dict(zip(ends, bt.month_ends(market.adj.days)[-len(ends) :], strict=True))
    clean, leaky = [], []
    for t in ends:
        today, fwd, fwd_leak = [], [], []
        for s in uni.values():
            i, j = s.index_of[t], s.index_of[nxt[t]]
            c = s.adj.close
            today.append(c[i] / c[i - 1] - 1)
            fwd.append(c[j] / c[i] - 1)
            fwd_leak.append(c[j] / c[i - 1] - 1)
        clean.append(study.stats.spearman(today, fwd))
        leaky.append(study.stats.spearman(today, fwd_leak))
    assert abs(float(np.mean(clean))) < 0.06 and float(np.mean(leaky)) > 0.14
    assert float(np.mean(leaky)) == pytest.approx(0.21, abs=0.07)

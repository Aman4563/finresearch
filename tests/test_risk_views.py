"""Risk tab views (#349 rolling and benchmark-relative statistics, #423 stress scenarios that never show an unknown
move as 0 %). Synthetic series only; expected values are computed independently (numpy or by hand)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from finresearch.portfolio.history import Position


# --------------------------------------------------------------------------- #423 stress scenarios
def _pos(key, tax_class, value=1000.0):
    return Position(key, key, "stock" if tax_class == "equity" else "mf", tax_class, [1], 10, 10, None, value, None,
                    key.split(":")[1])  # fmt: skip


def _fall_history():
    """NIFTYBEES and a stock whose daily returns are exactly 2x the index's (beta 2), 200 days to `end`."""
    end = date(2026, 10, 20)
    days = [end - timedelta(days=199 - i) for i in range(200)]
    bench, own = {days[0]: 100.0}, {days[0]: 50.0}
    for i in range(1, 200):
        rb = 0.01 * math.sin(i * 0.7)
        bench[days[i]] = bench[days[i - 1]] * (1 + rb)
        own[days[i]] = own[days[i - 1]] * (1 + 2 * rb)
    short = {d: 20.0 + i for i, d in enumerate(days[-10:])}  # 9 returns: too short for a beta
    return SimpleNamespace(
        days=days, benchmark=SimpleNamespace(closes=bench, factors={}),
        positions=[_pos("NSE:LONG", "equity"), _pos("NSE:NEWIPO", "equity"), _pos("MF:7", "debt_mf")],
        closes={"NSE:LONG": own, "NSE:NEWIPO": short}, factors={},
    )  # fmt: skip


def test_hypothetical_fall_leaves_short_history_and_non_equity_unmapped_by_default_423():
    from finresearch.portfolio.analytics import hypothetical_fall

    res = hypothetical_fall(_fall_history())
    rows = {x["key"]: x for x in res["positions"]}
    assert rows["NSE:LONG"]["move"] == pytest.approx(2 * -0.20, abs=1e-4)
    for k in ("NSE:NEWIPO", "MF:7"):  # unknown, never 0 % and never an unasked-for beta of 1
        assert rows[k]["move"] is None and rows[k]["inr"] is None and rows[k]["how"].startswith("unmapped")
    assert res["mapped_value"] == 1000 and res["unmapped_value"] == 2000
    assert res["portfolio_move"] == pytest.approx(-0.40, abs=1e-4) and res["inr"] == pytest.approx(
        -400, abs=1
    )
    assert res["assume_beta1"] is False


def test_hypothetical_fall_beta_one_is_a_labelled_option_for_short_history_equity_only_423():
    from finresearch.portfolio.analytics import hypothetical_fall

    res = hypothetical_fall(_fall_history(), assume_beta1=True)
    rows = {x["key"]: x for x in res["positions"]}
    assert rows["NSE:NEWIPO"]["move"] == pytest.approx(-0.20) and "assumed" in rows["NSE:NEWIPO"]["how"]
    assert rows["NSE:NEWIPO"]["assumed"] is True and rows["NSE:LONG"].get("assumed") is False
    assert rows["MF:7"]["move"] is None  # the option never reaches a debt fund
    assert res["mapped_value"] == 2000 and res["unmapped_value"] == 1000
    assert res["portfolio_move"] == pytest.approx((-400 - 200) / 2000, abs=1e-4)
    assert res["assume_beta1"] is True


def test_legacy_2008_scenario_is_relabelled_as_an_equity_only_proxy_423():
    from finresearch.portfolio.analytics import SCENARIOS

    gfc = next(s for s in SCENARIOS if s.id == "gfc2008")
    assert "equity only" in gfc.label.lower() and "unmapped" in gfc.note and "0 %" not in gfc.note


# --------------------------------------------------------------------------- #349 maths (expected values from numpy or by hand)
def _pair(n=300, seed=3):
    import numpy as np

    rng = np.random.default_rng(seed)
    rb = rng.normal(0.0004, 0.01, n)
    rp = 1.3 * rb + rng.normal(0.0002, 0.006, n)
    return [float(x) for x in rp], [float(x) for x in rb]


def test_information_ratio_is_annualised_active_return_over_tracking_error():
    import numpy as np

    from finresearch.portfolio import analytics_math as am

    rp, rb = _pair()
    a = np.array(rp) - np.array(rb)
    assert am.information_ratio(rp, rb) == pytest.approx(
        a.mean() * 252 / (a.std(ddof=1) * math.sqrt(252)), rel=1e-9
    )


def test_capture_ratios_use_geometric_means_on_up_and_down_days():
    import numpy as np

    from finresearch.portfolio import analytics_math as am

    rp, rb = _pair()
    p, b = np.array(rp), np.array(rb)

    def g(x):
        return float(np.prod(1 + x) ** (1 / len(x)) - 1)

    up, down = am.capture_ratios(rp, rb)
    assert up == pytest.approx(g(p[b > 0]) / g(b[b > 0]), rel=1e-9)
    assert down == pytest.approx(g(p[b < 0]) / g(b[b < 0]), rel=1e-9)
    assert up > 1 and down > 1  # beta 1.3: captures more of both


def test_capture_ratios_refuse_too_few_up_or_down_days():
    from finresearch.portfolio import analytics_math as am

    with pytest.raises(ValueError):
        am.capture_ratios([0.01] * 30, [0.02] * 30)  # no down day: unknown, never 0


def test_rolling_volatility_and_beta_match_numpy_per_window():
    import numpy as np

    from finresearch.portfolio import analytics_math as am

    rp, rb = _pair(n=200)
    rows = am.rolling_stats(rp, rb, window=126)
    assert len(rows) == 200 - 126 + 1
    last = rows[-1]
    p, b = np.array(rp[-126:]), np.array(rb[-126:])
    assert last.end == 199
    assert last.volatility == pytest.approx(p.std(ddof=1) * math.sqrt(252), rel=1e-9)
    assert last.beta == pytest.approx(np.cov(p, b, ddof=1)[0, 1] / b.var(ddof=1), rel=1e-9)
    assert am.rolling_stats(rp, None, window=126)[0].beta is None  # no benchmark: unknown, not 0
    assert am.rolling_stats(rp[:100], rb[:100], window=126) == []


def test_drawdown_episodes_hand_series():
    from finresearch.portfolio import analytics_math as am

    idx = [100, 90, 95, 100, 110, 99, 88, 105, 120, 108]
    eps = am.drawdown_episodes(idx, top=5)
    assert [(e.peak, e.trough, e.recovered) for e in eps] == [(4, 6, 8), (0, 1, 3), (8, 9, None)]
    assert [e.depth for e in eps] == pytest.approx([88 / 110 - 1, -0.10, -0.10])
    assert am.drawdown_episodes(idx, top=1)[0].depth == pytest.approx(-0.2)
    assert am.drawdown_episodes([1, 2, 3]) == []


def test_kupiec_and_christoffersen_hand_values():
    from finresearch.portfolio import analytics_math as am

    # T = 250, x = 20 at p = 0.05: LR = -2[230 ln .95 + 20 ln .05] + 2[230 ln .92 + 20 ln .08] = 4.03952; chi2(1) p .04445
    lr, pv = am.kupiec(250, 20, 0.05)
    assert lr == pytest.approx(4.039520, abs=1e-5) and pv == pytest.approx(0.044446, abs=1e-5)
    lr0, pv0 = am.kupiec(250, 0, 0.05)  # no exception at all: -2·250 ln .95 = 25.6466 (0·ln 0 = 0)
    assert lr0 == pytest.approx(25.646647, abs=1e-5) and pv0 < 1e-6
    # hits 0,1,1,0,0,0,1,0: n00 = 2, n01 = 2, n10 = 2, n11 = 1; π01 = 1/2, π11 = 1/3, π = 3/7
    # LR_ind = -2[4 ln(4/7) + 3 ln(3/7)] + 2[4 ln(1/2) + 2 ln(2/3) + ln(1/3)] = 0.196451
    assert am.christoffersen_ind([0, 1, 1, 0, 0, 0, 1, 0]) == pytest.approx(0.196451, abs=1e-5)


def test_var_backtest_is_walk_forward():
    import numpy as np

    from finresearch.portfolio import analytics_math as am

    rp, _ = _pair(n=400)
    hits = am.var_exceptions(rp, window=250)
    assert len(hits) == 150
    for k in (0, 77, 149):  # each day is tested against the VaR of the 250 days before it, never itself
        t = 250 + k
        q = -float(np.quantile(rp[t - 250 : t], 0.05))
        assert hits[k] == (rp[t] < -q)


# --------------------------------------------------------------------------- #349 payload
def _history(n=600, seed=11, benchmark=True):
    """A synthetic value history with no flows (so the TWR index is the value / its start) and a benchmark."""
    import numpy as np

    from finresearch.portfolio import analytics_math as am
    from finresearch.portfolio.history import Benchmark, History

    rng = np.random.default_rng(seed)
    days, d = [], date(2023, 1, 2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    rb = rng.normal(0.0004, 0.01, n - 1)
    rp = 1.2 * rb + rng.normal(0.0001, 0.007, n - 1)
    value = [100000.0, *(100000.0 * np.cumprod(1 + rp))]
    level = [1.0, *np.cumprod(1 + rb)]
    rets = am.twr_returns(value, [0.0] * n)
    bm = Benchmark(days, [float(x) for x in level], {}, {}, {}) if benchmark else None
    return History(days=days, value=[float(x) for x in value], flow=[0.0] * n, returns=rets, index=am.chain(rets),
                   benchmark=bm), [float(x) for x in rp], [float(x) for x in rb]  # fmt: skip


def _risk(h):
    from finresearch.portfolio.analytics import RiskFree, risk

    return risk(h, RiskFree(0.065, "test", None, None), {})


def test_risk_payload_benchmark_relative_lines_match_numpy_349():
    import numpy as np

    h, rp, rb = _history()
    real = _risk(h)["realised"]
    a = np.array(rp) - np.array(rb)
    assert real["information_ratio"]["value"] == pytest.approx(
        a.mean() * 252 / (a.std(ddof=1) * math.sqrt(252)), abs=1e-4
    )
    p, b = np.array(rp), np.array(rb)

    def g(x):
        return float(np.prod(1 + x) ** (1 / len(x)) - 1)

    assert real["up_capture"]["value"] == pytest.approx(g(p[b > 0]) / g(b[b > 0]), abs=1e-4)
    assert real["down_capture"]["value"] == pytest.approx(g(p[b < 0]) / g(b[b < 0]), abs=1e-4)
    assert real["active_return"]["value"] == pytest.approx(a.mean() * 252, abs=1e-4)


def test_risk_payload_rolling_and_drawdown_table_349():
    import numpy as np

    from finresearch.portfolio import analytics_math as am

    h, rp, rb = _history()
    out = _risk(h)
    roll = out["realised"]["rolling"]
    assert roll["window"] == 126 and len(roll["rows"]) == len(rp) - 126 + 1
    last = roll["rows"][-1]
    assert last["day"] == h.days[-1].isoformat()
    assert last["volatility"] == pytest.approx(np.std(rp[-126:], ddof=1) * math.sqrt(252), abs=1e-4)
    assert last["beta"] == pytest.approx(
        np.cov(rp[-126:], rb[-126:], ddof=1)[0, 1] / np.var(rb[-126:], ddof=1), abs=1e-4
    )
    dds = out["realised"]["drawdowns"]
    eps = am.drawdown_episodes(h.index, top=5)
    assert len(dds) == len(eps) <= 5
    assert dds[0]["depth"] == pytest.approx(out["realised"]["max_drawdown"]["value"], abs=1e-4)
    assert [x["peak"] for x in dds] == [h.days[e.peak].isoformat() for e in eps]
    assert all(x["recovered"] is None or x["days_to_recover"] >= 0 for x in dds)


def test_risk_payload_var_backtest_walk_forward_349():
    import numpy as np

    from finresearch.portfolio import analytics_math as am

    h, rp, _ = _history()
    vb = _risk(h)["realised"]["var_backtest"]
    tested = len(rp) - 250
    hits = [rp[t] < float(np.quantile(rp[t - 250 : t], 0.05)) for t in range(250, len(rp))]
    assert vb["days"] == tested and vb["exceptions"] == sum(hits)
    assert vb["expected"] == pytest.approx(tested * 0.05, abs=0.01)
    assert vb["p_uc"] == pytest.approx(am.kupiec(tested, sum(hits), 0.05)[1], abs=1e-4)
    lr_cc = am.kupiec(tested, sum(hits), 0.05)[0] + am.christoffersen_ind(hits)
    assert vb["p_cc"] == pytest.approx(math.exp(-lr_cc / 2), abs=1e-4)
    assert vb["verdict"] in {"consistent", "too_many", "too_few", "clustered"} and vb["note"]


def test_risk_payload_short_or_benchmarkless_history_is_unknown_never_zero_349():
    h, _, _ = _history(n=301)
    real = _risk(h)["realised"]
    assert real["var_backtest"]["reason"].startswith("needs 500") and real["var_backtest"].get("p_uc") is None
    assert real["information_ratio"]["value"] is not None  # 300 returns are enough for the relative lines
    h, _, _ = _history(n=100)
    real = _risk(h)["realised"]
    assert real["rolling"]["rows"] == [] and real["rolling"]["reason"].startswith("needs 126")
    assert real["information_ratio"]["value"] is None and real["up_capture"]["reason"]
    h, _, _ = _history(benchmark=False)
    real = _risk(h)["realised"]
    assert real["information_ratio"]["value"] is None and "NIFTYBEES" in real["information_ratio"]["reason"]
    assert real["rolling"]["rows"] and all(x["beta"] is None for x in real["rolling"]["rows"])


def test_var_verdict_is_consistent_only_when_the_joint_test_also_passes_349():
    from finresearch.portfolio.analytics import var_verdict

    assert var_verdict(0.5, 0.5, 0.5, x=13, expected=12.5) == "consistent"
    # each one-degree test passes but conditional coverage rejects: never shown as consistent
    assert var_verdict(0.07, 0.07, 0.04, x=20, expected=12.5) == "too_many"
    assert var_verdict(0.07, 0.07, 0.04, x=6, expected=12.5) == "too_few"
    assert var_verdict(0.01, 0.5, 0.03, x=25, expected=12.5) == "too_many"
    assert var_verdict(0.5, 0.01, 0.03, x=13, expected=12.5) == "clustered"


def test_information_ratio_with_zero_tracking_error_is_a_reason_not_an_error_349():
    from finresearch.portfolio.analytics import _relative

    rb = [0.01 * math.sin(i) for i in range(200)]
    out = _relative(list(rb), rb, 200)  # tracks the index exactly
    assert (
        out["information_ratio"]["value"] is None
        and "tracking error is zero" in out["information_ratio"]["reason"]
    )
    assert out["active_return"]["value"] == 0


# --------------------------------------------------------------------------- #399
def test_var_note_says_the_tail_count_is_in_sample_399():
    """'1 day in 20 lost more' over the VaR's own window is true by construction (#399): the note says so and points
    at the out-of-sample backtests instead of presenting it as a check."""
    h, _, _ = _history()
    note = _risk(h)["realised"]["var_1d"]["note"]
    assert "in-sample" in note and "by construction" in note and "backtest" in note


def test_risk_payload_carries_the_logged_var_backtest_399():
    from finresearch.portfolio.analytics import RiskFree, risk

    h, _, _ = _history()
    vl = {"level": 0.95, "resolved": 3, "reason": "needs 250 logged days with a known outcome; have 3"}
    assert risk(h, RiskFree(0.065, "t", None, None), {}, var_ledger=vl)["realised"]["var_ledger"] == vl
    assert "var_ledger" not in _risk(h)["realised"]  # not read: absent, never an empty pass

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

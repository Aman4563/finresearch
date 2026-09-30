"""Stock-signal arithmetic (fincalc.signals) and the backtest harness (evals.stock_backtest), offline: hand-computed
values and a small synthetic universe."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from finresearch.evals import stock_backtest as bt
from finresearch.fincalc import signals as sg


def test_wilson_interval_matches_the_roadmap_example():
    lo, hi = sg.wilson(7, 10)
    assert round(lo, 3) == 0.397 and round(hi, 3) == 0.892  # roadmap §C.10: 7/10 -> about 40-89 %
    assert sg.wilson(0, 0) is None and sg.wilson(0, 5)[0] == 0.0


def test_action_factors_for_bonuses_and_splits():
    assert sg.action_factor("Bonus 1:1") == 2 and sg.action_factor("Bonus 3:2") == 2.5
    assert (
        sg.action_factor("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share") == 5
    )
    assert (
        sg.action_factor("Face Value Split (Sub-Division) - From Re 1/- Per Share To Re 0.50/- Per Share")
        == 2
    )
    assert (
        sg.action_factor("Dividend - Rs 20 Per Share") is None
        and sg.action_factor("Rights 3:25 @ Premium") is None
    )


def test_split_adjustment_uses_the_observed_drop_and_flags_unexplained_jumps():
    days = [date(2018, 9, 1) + timedelta(days=i) for i in range(6)]
    close = [
        1400.0,
        1434.25,
        737.15,
        740.0,
        400.0,
        405.0,
    ]  # INFY's 1:1 bonus on day 2, then an unexplained -46 %
    a = sg.adjust_for_actions(days, close, [(days[2], "Bonus 1:1")])
    assert a.applied == [(days[2], 2.0)] and a.close[:2] == [700.0, 717.125] and a.close[2] == 737.15
    assert a.anomalies == [days[4]]
    # a split and a bonus on the same ex-date compound (BAJAJFINSV, Sep-2022: 5x split and 1:1 bonus); repeats count once
    c = sg.adjust_for_actions(
        days[:3],
        [1600.0, 1650.0, 166.0],
        [
            (days[1], "Face Value Split (Sub-Division) - From Rs 5/- Per Share To Re 1/- Per Share"),
            (days[1], "Bonus 1:1"),
            (days[1], "Bonus 1:1"),
        ],
    )
    assert c.applied == [(days[2], 10.0)] and c.close[:2] == [160.0, 165.0] and c.anomalies == []
    # an action with no matching price drop is not applied
    b = sg.adjust_for_actions(days[:2], close[:2], [(days[1], "Bonus 1:1")])
    assert b.applied == [] and b.close == close[:2]


def test_trend_momentum_volatility_and_52_week_position():
    c = [100.0 * 1.001**i for i in range(300)]
    assert sg.sma([1, 2, 3, 4], 2) == 3.5 and sg.sma([1.0], 2) is None
    assert math.isclose(sg.momentum_12_1(c), 1.001 ** (299 - 21) / 1.001 ** (299 - 252) - 1)
    assert sg.momentum_12_1(c[:252]) is None
    assert sg.realised_vol(c) < 1e-9  # a constant daily return has no volatility
    alt = [100.0 * (1.01 if i % 2 else 1.0) for i in range(260)]  # +1 %, -0.99 % alternating
    assert 0.15 < sg.realised_vol(alt) < 0.17
    assert sg.range_position(150, 100, 200) == 0.5 and sg.range_position(1, 2, 2) is None
    assert sg.percentile_rank([10, 20, 30, 40], 25) == 0.5


def test_atr_is_wilders_average_true_range():
    high = [10, 11, 12, 11, 13]
    low = [9, 10, 10, 9, 11]
    close = [9.5, 10.5, 11, 10, 12.5]
    # TRs from day 1: max(1, 1.5, .5)=1.5, max(2, 1.5, .5)=2, max(2, 0, 2)=2, max(2, 3, 1)=3
    assert sg.atr(high, low, close, period=3) == pytest.approx(((1.5 + 2 + 2) / 3 * 2 + 3) / 3)
    assert sg.atr(high, low, close, period=14) is None


def test_pe_percentile_uses_only_eps_known_on_each_day():
    q = [(date(2025, 1, 15), 10.0), (date(2025, 4, 15), 10.0), (date(2025, 7, 15), 10.0), (date(2025, 10, 15), 10.0),
         (date(2026, 1, 15), 15.0)]  # fmt: skip
    ttm = sg.ttm_eps_series(q)
    assert ttm == [(date(2025, 10, 15), 40.0), (date(2026, 1, 15), 45.0)]
    days = [date(2025, 10, 14), date(2025, 10, 15), date(2026, 1, 16)]
    assert sg.pe_series(days, [800.0, 800.0, 900.0], ttm) == [20.0, 20.0]


def test_position_size_is_vol_scaled_then_capped_never_raised_by_kelly():
    s = sg.position_size(0.25, risk_budget=0.02, cap=0.10)
    assert s["weight"] == pytest.approx(0.08) and s["binding"] == "volatility"
    assert sg.position_size(0.10, risk_budget=0.02, cap=0.05)["binding"] == "profile cap"
    k = sg.position_size(0.25, risk_budget=0.02, cap=0.10, mean_excess=0.01)  # f* = 0.16, a quarter = 0.04
    assert k["weight"] == pytest.approx(0.04) and k["binding"] == "quarter-Kelly ceiling"
    assert sg.position_size(0.25, risk_budget=0.02, cap=0.10, mean_excess=0.5)["weight"] == pytest.approx(
        0.08
    )
    assert sg.position_size(None, risk_budget=0.02, cap=0.1)["weight"] is None


# --------------------------------------------------------------------------- backtest harness on synthetic data
def synthetic(n_stocks: int = 12, years: int = 3):
    start = date(2020, 1, 1)
    days = [
        start + timedelta(days=i)
        for i in range(int(years * 365.25))
        if (start + timedelta(days=i)).weekday() < 5
    ]
    universe = {}
    for k in range(n_stocks):
        drift = (k - 4.5) * 0.0004  # stocks 0..4 fall, 5..11 rise, 11 fastest
        close = [100 * math.exp(drift * i) * (1 + 0.01 * ((i + k) % 2)) for i in range(len(days))]
        universe[f"S{k:02d}"] = bt.Series(f"S{k:02d}", sg.adjust_for_actions(days, close, []))
    index = [(d, 1000 * math.exp(0.0002 * i)) for i, d in enumerate(days)]
    return universe, index


def test_backtest_holds_the_top_quintile_in_an_uptrend_and_charges_costs():
    universe, index = synthetic()
    res = bt.run(universe, index)
    months = res["months"]
    assert months, "the harness produced no months"
    first = months[0]
    # 12 names -> k = round(12/5) = 2: the two fastest risers, equally weighted
    assert first["held"] == ["S10", "S11"] and first["universe"] == 12 and first["qualifying"] == 7
    # the first rebalance buys 100 % of the book: turnover 0.5 (one-way), cost = 1.0 x the buy cost
    assert first["turnover"] == pytest.approx(0.5) and first["cost"] == pytest.approx(bt.COST_BUY)
    r = [bt._ret(universe[s], date.fromisoformat(first["month_end"]), date.fromisoformat(first["next"]))
         for s in ("S10", "S11")]  # fmt: skip
    assert first["strategy"] == pytest.approx(sum(r) / 2 - bt.COST_BUY)
    st = res["stats"]
    assert st["cagr"]["strategy"] > st["cagr"]["equal_weight_universe"]  # it holds the strongest trends
    assert st["monthly_hit_rate_vs_equal_weight"]["n"] == len(months)
    b = res["buckets"]
    assert b["uptrend / strong momentum"]["n"] > 0 and b["downtrend / negative momentum"]["n"] > 0
    assert b["uptrend / strong momentum"]["p"] == 1.0 and b["downtrend / negative momentum"]["p"] == 0.0
    assert b["all"]["n"] == sum(v["n"] for k, v in b.items() if k != "all")


def test_backtest_statistics_golden():
    assert bt.cagr([0.1, -0.1]) == pytest.approx(0.99**6 - 1)
    assert bt.max_drawdown([0.1, -0.5, 0.2]) == pytest.approx(-0.5)
    assert bt.newey_west_t([0.01] * 20) is None  # no variance
    assert bt.bucket_of(True, 1.2) == "uptrend / strong momentum" and bt.bucket_of(False, -0.1).startswith(
        "down"
    )


# --------------------------------------------------------------------------- TTM EPS from what the company files
def _p(a: str, b: str, eps: float, known: str, cons: bool | None = True) -> sg.FiledPeriod:
    return sg.FiledPeriod(date.fromisoformat(a), date.fromisoformat(b), eps, date.fromisoformat(known), cons)


def test_ttm_from_four_quarters_is_their_sum():
    q = [_p("2025-04-01", "2025-06-30", 1, "2025-07-20"), _p("2025-07-01", "2025-09-30", 2, "2025-10-20"),
         _p("2025-10-01", "2025-12-31", 3, "2026-01-20"), _p("2026-01-01", "2026-03-31", 4, "2026-04-20")]  # fmt: skip
    (row,) = sg.ttm_from_periods(q)
    assert (row.end, row.eps, row.known, row.basis) == (
        date(2026, 3, 31),
        10,
        date(2026, 4, 20),
        "TTM from four quarters",
    )


def test_ttm_from_two_half_years_for_a_half_yearly_filer():
    # H1 from the September filing's year-to-date, H2 from March's six-month "current period": 3 + 7 = 10
    h = [_p("2025-04-01", "2025-09-30", 3, "2025-11-10"), _p("2025-10-01", "2026-03-31", 7, "2026-05-09")]
    (row,) = sg.ttm_from_periods(h)
    assert (row.eps, row.basis, row.known) == (10, "TTM from two half-years", date(2026, 5, 9))


def test_fiscal_year_eps_is_preferred_at_the_year_end_and_four_quarters_otherwise():
    fy = _p(
        "2025-04-01", "2026-03-31", 10.2, "2026-05-09"
    )  # the year's own EPS (weighted shares) beats a sum
    halves = [
        _p("2025-04-01", "2025-09-30", 3, "2025-11-10"),
        _p("2025-10-01", "2026-03-31", 7, "2026-05-09"),
    ]
    (row,) = sg.ttm_from_periods([*halves, fy])
    assert (row.eps, row.basis) == (10.2, "last fiscal year EPS")


def test_ttm_from_a_half_year_and_two_quarters():
    # a half-yearly filer at June: Jul-Sep quarter + Oct-Mar half + Apr-Jun quarter = 2 + 7 + 5 = 14
    p = [_p("2025-07-01", "2025-09-30", 2, "2025-11-10"), _p("2025-10-01", "2026-03-31", 7, "2026-05-09"),
         _p("2026-04-01", "2026-06-30", 5, "2026-08-05")]  # fmt: skip
    (row,) = sg.ttm_from_periods(p)
    assert (row.end, row.eps, row.basis) == (
        date(2026, 6, 30),
        14,
        "TTM from a quarter + a half-year + a quarter",
    )


def test_no_ttm_when_a_quarter_is_missing_and_the_gap_is_named():
    fy = _p("2025-04-01", "2026-03-31", 41.65, "2026-05-09")
    h2 = _p("2025-10-01", "2026-03-31", 11.48, "2026-05-09")
    q1 = _p("2026-04-01", "2026-06-30", 18.39, "2026-08-05")
    rows = sg.ttm_from_periods([fy, h2, q1])
    # only the fiscal year tiles twelve months; June 2026 needs Jul-Sep 2025 on its own
    assert [(r.end, r.eps, r.basis) for r in rows] == [(date(2026, 3, 31), 41.65, "last fiscal year EPS")]
    assert sg.ttm_chains([fy, h2, q1], date(2026, 6, 30)) == []
    assert sg.ttm_gaps([fy, h2, q1], date(2026, 6, 30)) == [(date(2025, 7, 1), date(2025, 9, 30))]
    assert sg.ttm_gaps([h2], date(2026, 3, 31)) == [(date(2025, 4, 1), date(2025, 9, 30))]
    assert sg.ttm_from_periods([h2, q1]) == []


def test_ttm_keeps_one_basis_and_never_goes_back_in_time():
    cons = [_p("2025-04-01", "2025-09-30", 3, "2025-11-10"), _p("2025-10-01", "2026-03-31", 7, "2026-05-09")]
    alone = [_p("2025-04-01", "2025-09-30", 90, "2025-11-09", cons=False),
             _p("2025-10-01", "2026-03-31", 10, "2026-05-08", cons=False)]  # fmt: skip
    (row,) = sg.ttm_from_periods([*cons, *alone])
    assert row.eps == 10 and {p.consolidated for p in row.pieces} == {True}  # consolidated first, never mixed
    (row,) = sg.ttm_from_periods([cons[1], alone[0]])
    assert row.eps == 97  # a mixed-basis chain only when no single-basis one exists
    (row,) = sg.ttm_from_periods(alone)
    assert row.eps == 100
    # an older year learned after a newer one never replaces it
    late = sg.ttm_from_periods([_p("2025-04-01", "2026-03-31", 10, "2026-05-09"),
                                _p("2024-04-01", "2025-03-31", 8, "2026-06-01")])  # fmt: skip
    assert [r.end for r in late] == [date(2026, 3, 31)]

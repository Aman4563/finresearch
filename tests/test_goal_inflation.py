"""CPI inflation for the goal planner (#347): chaining MoSPI's 2012-base index into the 2024-base series by the
official year-on-year rates, trailing / long-run rates and the year-to-year spread. Synthetic numbers only."""

from __future__ import annotations

import math
from datetime import date

import pytest

from finresearch.wealth import inflation as inf


def synthetic_old(first: int = 2013, last: int = 2025, growth: float = 0.05) -> dict[date, float]:
    """A made-up 2012-base index growing exactly `growth` a year (monthly compounding)."""
    return {
        date(y, m, 1): 100 * (1 + growth) ** ((y - first) + (m - 1) / 12)
        for y in range(first, last + 1)
        for m in range(1, 13)
    }


def synthetic_new_yoy(months: int = 9, pct: float = 3.0) -> dict[date, float]:
    return {date(2026, m, 1): pct for m in range(1, months + 1)}


def test_chain_extends_old_index_by_official_yoy():
    old = synthetic_old()
    lv = inf.chain(old, synthetic_new_yoy())
    # Sep-2026 = Sep-2025 x 1.03 (the 2024-base series' own year-on-year rate), no base-linking factor needed
    assert lv[date(2026, 9, 1)] == pytest.approx(old[date(2025, 9, 1)] * 1.03, rel=1e-12)
    assert max(lv) == date(2026, 9, 1)


def test_chain_stops_at_a_gap_in_the_new_series():
    yoy = synthetic_new_yoy()
    del yoy[date(2026, 5, 1)]
    lv = inf.chain(synthetic_old(), yoy)
    assert max(lv) == date(2026, 4, 1)  # never skips a month (a gap would silently stretch the window)


def test_stats_trailing_long_run_and_spread_hand_computed():
    lv = inf.chain(synthetic_old(), synthetic_new_yoy())
    st = inf.stats(lv, years=10)
    assert st.as_of == date(2026, 9, 1)
    assert st.trailing_pct == pytest.approx(3.0, abs=1e-9)
    # Sep-2016 -> Sep-2026: nine years at 5 % then one at 3 %
    assert st.long_run_pct == pytest.approx(((1.05**9 * 1.03) ** 0.1 - 1) * 100, abs=1e-9)
    # annual readings 5 x9 and 3: mean 4.8, squares 9 x 0.04 + 3.24 = 3.6, sample variance 3.6 / 9 = 0.4
    assert st.sd_pct == pytest.approx(math.sqrt(0.4), abs=1e-9)
    assert st.years == 10 and st.start == date(2016, 9, 1)


def test_stats_too_short_history_is_unknown_with_reason():
    lv = inf.chain(synthetic_old(first=2022), synthetic_new_yoy())
    st = inf.stats(lv, years=10)
    assert st.long_run_pct is None and st.sd_pct is None
    assert "10 years" in st.why
    assert st.trailing_pct == pytest.approx(3.0, abs=1e-9)  # the part that is known stays known


def test_stats_empty_is_unknown():
    st = inf.stats({}, years=10)
    assert st.as_of is None and st.trailing_pct is None and st.long_run_pct is None and st.why


# --------------------------------------------------------------------------- inflation paths in the Monte Carlo
from finresearch.wealth import goals  # noqa: E402

KW = dict(start=500_000.0, sip0=15_000.0, step_up_pct=5.0, months=120, target_today=3_000_000.0,
          inflation_pct=6.0, equity_pct=None, gold_pct=10.0, assumptions=goals.DEFAULT_ASSUMPTIONS, n=2000,
          seed=11)  # fmt: skip


def test_growth_from_draws_compounds_whole_and_part_years_hand_computed():
    import numpy as np

    g = goals.growth_from_draws(np.array([[0.10, 0.20], [0.0, -0.10]]), months=18)
    assert g == pytest.approx([1.10 * 1.20**0.5, 1.0 * 0.90**0.5], rel=1e-12)


def test_zero_spread_keeps_the_fixed_inflation_plan_exactly():
    a, b = goals.plan(**KW), goals.plan(**KW, inflation_sd_pct=0.0)
    assert a.to_json() == b.to_json()
    assert b.target_nominal == pytest.approx(3_000_000 * 1.06**10)


def test_inflation_spread_reuses_the_return_draws_and_spreads_the_target():
    a, b = goals.plan(**KW), goals.plan(**KW, inflation_sd_pct=1.5)
    assert b.terminal_pcts == a.terminal_pcts  # returns drawn from their own stream: common random numbers
    assert b.target_nominal == pytest.approx(a.target_nominal)  # the central target is the long-run rate's
    lo, mid, hi = (b.target_pcts[k] for k in ("p10", "p50", "p90"))
    assert lo < mid < hi and lo < a.target_nominal < hi
    # mean target ~ central: E[prod(1 + pi)] = (1 + mu)^10 for independent years; ~1.5 % sd of 2,000 paths
    assert b.target_mean == pytest.approx(a.target_nominal, rel=0.01)
    assert b.inflation_sd_pct == 1.5


def test_inflation_sensitivity_rows_match_fixed_rate_runs():
    b = goals.plan(**KW, inflation_sd_pct=1.5, sensitivity=[("CPI trailing 12 months", 3.0), ("+2 pp", 8.0)])
    rows = {r["label"]: r for r in b.inflation_sensitivity}
    for label, rate in (("CPI trailing 12 months", 3.0), ("+2 pp", 8.0)):
        fixed = goals.plan(**(KW | {"inflation_pct": rate}), extras=False)
        assert rows[label]["inflation_pct"] == rate
        assert rows[label]["p_success"] == pytest.approx(fixed.p_success)
    assert rows["+2 pp"]["p_success"] <= rows["CPI trailing 12 months"]["p_success"]

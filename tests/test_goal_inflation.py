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

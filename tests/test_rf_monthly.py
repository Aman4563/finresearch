"""#479: fund Sharpe/Sortino against each month's own risk-free rate (fincalc.riskfree monthly series, fincalc.funds
excess-return ratios) beside the factsheet-style figure (one overnight rate on the last working day for every month).
Expected values are hand-computed below from the stated convention, rf_m = (1 + y_m x 91/365)^(d/91) - 1, with y_m
the month's average 91-day yield; the repo was 5.25 % until the 07-Oct-2026 hike to 5.50 %."""

from __future__ import annotations

import math
import statistics as st
from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import funds as F
from finresearch.fincalc import riskfree as R

OCT_AVG = (6 * 0.0525 + 25 * 0.055) / 31  # 1-6 Oct at 5.25 %, 7-31 Oct at 5.50 %: 5.4516 %


def month_rf(y: float, days: int) -> float:
    return (1 + y * 91 / 365) ** (days / 91) - 1


def test_each_month_averages_its_own_daily_91_day_rate():
    months = R.month_rates(date(2026, 7, 15), date(2026, 10, 31))
    assert list(months) == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    jul, octo = months[date(2026, 7, 1)], months[date(2026, 10, 1)]
    assert jul.days == 31 and jul.y91 == pytest.approx(0.0525)  # the whole month, not just from the 15th
    assert octo.days == 31 and octo.y91 == pytest.approx(OCT_AVG, abs=1e-12)
    assert dict(octo.tiers) == {"proxy": 31}


def test_the_last_month_is_averaged_only_up_to_the_end_day():
    octo = R.month_rates(date(2026, 10, 1), date(2026, 10, 10))[date(2026, 10, 1)]
    assert octo.days == 10 and octo.y91 == pytest.approx((6 * 0.0525 + 4 * 0.055) / 10, abs=1e-12)


def test_accrual_over_a_whole_month_is_the_monthly_rate():
    months = R.month_rates(date(2026, 7, 1), date(2026, 10, 31))
    # (30-Jun, 31-Jul] is July's 31 days
    assert R.accrual(date(2026, 6, 30), date(2026, 7, 31), months) == pytest.approx(
        month_rf(0.0525, 31), abs=1e-15
    )
    assert (
        R.accrual(date(2026, 6, 29), date(2026, 7, 31), months) is None
    )  # 30-Jun: no June in the months given
    assert month_rf(0.0525, 31) == pytest.approx(0.0044398017, abs=1e-10)  # by hand
    # three days across a month end: 30-Sep at September's rate, 1-2 Oct at October's
    got = R.accrual(date(2026, 9, 29), date(2026, 10, 2), months)
    want = math.exp(math.log(1 + 0.0525 * 91 / 365) / 91 + 2 * math.log(1 + OCT_AVG * 91 / 365) / 91) - 1
    assert got == pytest.approx(want, abs=1e-15)


def test_a_month_before_the_policy_table_is_unknown():
    months = R.month_rates(date(2014, 12, 1), date(2015, 1, 31))
    assert months[date(2014, 12, 1)].y91 is None and "01-Jan-2015" in months[date(2014, 12, 1)].note
    assert months[date(2015, 1, 1)].y91 is not None
    assert R.accrual(date(2014, 12, 30), date(2015, 1, 2), months) is None  # 31-Dec-2014 is unknown
    assert R.accrual(date(2014, 12, 31), date(2015, 1, 2), months) is not None  # 1-2 Jan 2015 only


def test_owner_rates_enter_the_monthly_average_from_their_day():
    own = [R.OwnerRate(date(2026, 10, 14), 91, Decimal("0.057281"))]
    octo = R.month_rates(date(2026, 10, 1), date(2026, 10, 31), own)[date(2026, 10, 1)]
    assert octo.y91 == pytest.approx((6 * 0.0525 + 7 * 0.055 + 18 * 0.057281) / 31, abs=1e-12)
    assert dict(octo.tiers) == {"proxy": 13, "owner": 18}


# ------------------------------------------------------------------ the ratios on a synthetic series
NAVS = [(date(2026, 6, 30), Decimal(100)), (date(2026, 7, 31), Decimal(102)), (date(2026, 8, 31), Decimal(101)),
        (date(2026, 9, 30), Decimal(104)), (date(2026, 10, 31), Decimal(103))]  # fmt: skip
RF = [month_rf(0.0525, 31), month_rf(0.0525, 31), month_rf(0.0525, 30), month_rf(OCT_AVG, 31)]


def test_monthly_sharpe_and_sortino_subtract_each_months_own_rate():
    r = [102 / 100 - 1, 101 / 102 - 1, 104 / 101 - 1, 103 / 104 - 1]
    e = [a - b for a, b in zip(r, RF, strict=True)]
    sharpe = st.mean(e) / st.stdev(e) * math.sqrt(12)
    dd = math.sqrt(sum(min(0.0, x) ** 2 for x in e) / len(e))
    sortino = st.mean(e) * 12 / (dd * math.sqrt(12))
    assert (sharpe, sortino) == (pytest.approx(0.52945114, abs=1e-8), pytest.approx(1.07536896, abs=1e-8))
    assert float(F.sharpe_ratio_excess(NAVS, RF, 12)) == pytest.approx(sharpe, abs=1e-12)
    assert float(F.sortino_ratio_excess(NAVS, RF, 12)) == pytest.approx(sortino, abs=1e-12)


def test_the_factsheet_style_figure_uses_one_rate_for_every_month_and_differs():
    # repo 5.50 % on the last working day, as an annual rate for all four months (AMFI's (Rp - Rf) shortcut)
    fs = float(F.sharpe_ratio(NAVS, Decimal("0.055"), 12))
    assert fs == pytest.approx(0.50872919, abs=1e-8)
    assert float(F.sortino_ratio(NAVS, Decimal("0.055"), 12)) == pytest.approx(1.02398390, abs=1e-8)
    assert abs(fs - float(F.sharpe_ratio_excess(NAVS, RF, 12))) > 0.02


def test_excess_ratios_need_one_rate_per_period():
    with pytest.raises(ValueError, match="one risk-free return per period"):
        F.sharpe_ratio_excess(NAVS, RF[:3], 12)

"""#479: signals.rates' monthly series and the factsheet-style rate, as the fund and portfolio pages show them. The
owner tier is set through `rates.OWNER` (no database)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import riskfree as R
from finresearch.signals import rates


def test_the_factsheet_rate_is_the_repo_in_force_on_the_last_working_day():
    f = rates.factsheet(date(2026, 10, 30))
    assert f["known"] is True and f["rate"] == pytest.approx(0.055) and f["as_of"] == "2026-10-30"
    assert (
        "5.50 %" in f["source"]
        and "07-Oct-2026" in f["source"]
        and f["url"].startswith("https://www.rbi.org.in/")
    )
    assert "factsheet" in f["label"]
    assert rates.factsheet(date(2026, 10, 6))["rate"] == pytest.approx(0.0525)
    old = rates.factsheet(date(2014, 12, 31))
    assert old["known"] is False and old["rate"] is None and "01-Jan-2015" in old["note"]


def test_the_monthly_series_says_which_tiers_it_used(monkeypatch):
    monkeypatch.setattr(rates, "OWNER", lambda: [R.OwnerRate(date(2026, 10, 14), 91, Decimal("0.057281"))])
    months, why = rates.months_for(date(2026, 9, 15), date(2026, 10, 31))
    assert why is None and list(months) == [date(2026, 9, 1), date(2026, 10, 1)]
    info = rates.monthly_json(months)
    assert info["method"] == "monthly" and info["tiers"] == {"proxy": 30 + 13, "owner": 18}
    assert info["months"][-1] == {"month": "2026-10", "days": 31,
                                  "y91": pytest.approx((6 * 0.0525 + 7 * 0.055 + 18 * 0.057281) / 31),
                                  "tiers": {"proxy": 13, "owner": 18}, "stale_days": 0}  # fmt: skip
    assert info["unknown"] == [] and "91-day" in info["label"]
    rf = rates.accrual_fn(months)
    assert rf(date(2026, 9, 30), date(2026, 10, 31)) == pytest.approx(
        (1 + info["months"][-1]["y91"] * 91 / 365) ** (31 / 91) - 1, abs=1e-15
    )


def test_a_month_before_the_table_is_listed_as_unknown():
    months, _ = rates.months_for(date(2014, 12, 1), date(2015, 1, 31))
    info = rates.monthly_json(months)
    assert info["unknown"] == [{"month": "2014-12", "note": months[date(2014, 12, 1)].note}]

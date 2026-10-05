"""portfolio.limits: one position limit (#194) and one rebalancing band (#195); expectations computed by hand."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from finresearch.portfolio import limits
from finresearch.portfolio.rebalance import Lot, Position, plan
from finresearch.portfolio.tax import HoldingTax
from finresearch.suggest.profile import Profile
from finresearch.wealth import allocation


# --------------------------------------------------------------------------- band (#195)
def test_band_is_the_tighter_rule():
    assert limits.band_pp(60) == 5  # min(5, 15)
    assert limits.band_pp(20) == 5  # min(5, 5)
    assert limits.band_pp(10) == 2.5  # min(5, 2.5)
    assert limits.band_pp(0) == 5  # 0 % target: absolute only
    assert limits.band_pp(10, 5, 0) == 5  # relative band switched off
    assert limits.band_pp(10, 2, 25) == 2  # min(2, 2.5)
    assert limits.outside_band(12.6, 10) and not limits.outside_band(12.5, 10)  # |2.6| > 2.5; |2.5| is not


def test_bands_come_from_the_profile():
    assert limits.bands(Profile()) == (5.0, 25.0)
    assert limits.bands(Profile(rebalance_band_abs_pp=D(3), rebalance_band_rel_pct=D(0))) == (3.0, 0.0)
    assert "tighter of ±5 pp and 25 %" in limits.band_rule(5, 25)


def _pos(hid, cls, price, units, asset_type):
    ht = HoldingTax(hid, f"Example {hid}", "Demat", f"INE000X0{hid:04d}", "equity" if asset_type == "stock"
                    else "debt_mf", True, None)  # fmt: skip
    lots = [Lot(date(2024, 1, 1), D(units), D(price))]
    return Position(ht, asset_type, cls, D(price) * D(units), D(price), lots)


def test_wealth_page_and_rebalance_card_agree_on_a_breach():
    # ₹87,000 equity + ₹13,000 debt against 90 / 10: equity drifts -3 pp (band min(5, 22.5) = 5: inside), debt +3 pp
    # (band min(5, 2.5) = 2.5: OUTSIDE). The old /wealth rule max(5, 2.5) = 5 called debt inside: the two disagreed.
    wealth = {r["label"]: r for r in allocation.compare({"Equity": 87.0, "Debt": 13.0},
                                                        {"Equity": 90.0, "Debt + cash": 10.0})}  # fmt: skip
    card = plan([_pos(1, "Stocks", 870, 100, "stock"), _pos(2, "Debt funds", 10, 1300, "mf")],
                {"Stocks": 90, "Debt funds": 10}, date(2026, 10, 5), [], D("0.30"))  # fmt: skip
    rows = {r["label"]: r for r in card["allocation"]}
    assert wealth["Equity"]["band_pp"] == rows["Stocks"]["band_pp"] == 5
    assert wealth["Debt + cash"]["band_pp"] == rows["Debt funds"]["band_pp"] == 2.5
    assert wealth["Equity"]["outside_band"] is rows["Stocks"]["outside_before"] is False
    assert wealth["Debt + cash"]["outside_band"] is rows["Debt funds"]["outside_before"] is True

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


# --------------------------------------------------------------------------- position limit (#194)
def test_position_limit_by_profile_then_risk():
    assert limits.position_limit(Profile(risk_appetite="low")).to_json() == {
        "pct": 5.0, "source": "risk", "risk": "low", "rule": "5 % — your risk profile: low"}  # fmt: skip
    assert limits.position_limit(Profile()).pct == 8.0  # medium is the default appetite
    assert limits.position_limit(Profile(risk_appetite="high")).pct == 10.0
    own = limits.position_limit(Profile(risk_appetite="low", max_position_pct=D("12.5")))
    assert (own.pct, own.source, own.rule) == (12.5, "profile", "12.5 % — your profile's max position")
    assert limits.position_limit(None).rule == "8 % — your risk profile: medium"  # no profile: medium default


def test_alert_catalogue_suggests_the_same_limit():
    from finresearch.alerts.registry import registry_json

    out = registry_json(limits.position_limit(Profile(risk_appetite="low")))
    metric = next(m for m in out["metrics"] if m["key"] == "max_position_pct" and m["kind"] == "portfolio")
    tpl = next(t for t in out["templates"] if t["id"] == "pf-concentration")
    assert metric["default_value"] == tpl["value"] == "5" and "5 % — your risk profile: low" in tpl["why"]
    plain = registry_json()  # without a profile: the medium default, never the old flat 10
    assert next(t for t in plain["templates"] if t["id"] == "pf-concentration")["value"] == "8"


# --------------------------------------------------------------------------- funds vs the single-stock limit (#200)
def test_a_diversified_fund_never_counts_against_the_single_stock_limit():
    from finresearch.portfolio.metrics import grouped_weights

    # medium profile (8 %): a 9 % flexi-cap fund and a 12 % Nifty ETF must not read as "position too big";
    # the largest single stock is the 4 % one (the same INE000X0 stock in two accounts adds up: 2.5 + 1.5)
    v = {"holdings": {
        "1": {"asset_type": "mf", "name": "Example Flexi Cap Fund - Direct Growth", "scheme_code": "100001",
              "weight_pct": 9.0},
        "2": {"asset_type": "stock", "name": "Example Nifty 50 ETF", "nse_symbol": "EXNIFTYBEES", "weight_pct": 12.0},
        "3": {"asset_type": "stock", "name": "Example Ltd", "nse_symbol": "EXAMPLE", "weight_pct": 2.5},
        "4": {"asset_type": "stock", "name": "Example Ltd", "nse_symbol": "EXAMPLE", "weight_pct": 1.5},
        "5": {"asset_type": "other", "name": "Example FD", "weight_pct": 30.0},
    }}  # fmt: skip
    stocks, funds = grouped_weights(v, "stock"), grouped_weights(v, "fund")
    assert stocks == {"EXAMPLE": (4.0, "Example Ltd")}
    assert funds == {"100001": (9.0, "Example Flexi Cap Fund - Direct Growth"),
                     "EXNIFTYBEES": (12.0, "Example Nifty 50 ETF")}  # fmt: skip
    assert max(w for w, _ in stocks.values()) < limits.position_limit(Profile()).pct  # 4 < 8: no alert
    assert len(grouped_weights(v)) == 4  # n_effective still counts every holding


def test_etf_recognition():
    assert limits.is_fund_like("mf")
    assert limits.is_fund_like("stock", "NIFTYBEES") and limits.is_fund_like("stock", "MON100", "Example ETF")
    assert not limits.is_fund_like("stock", "INFY", "Infosys Ltd")
    assert not limits.is_fund_like("other", None, "Example ETF FD")


def test_alert_catalogue_has_a_separate_fund_threshold():
    from finresearch.alerts.registry import registry_json

    out = registry_json(limits.position_limit(Profile(risk_appetite="medium")))
    fund = next(m for m in out["metrics"] if m["key"] == "max_fund_pct" and m["kind"] == "portfolio")
    assert fund["default_value"] == f"{limits.FUND_LIMIT_PCT:g}" == "25"
    tpl = next(t for t in out["templates"] if t["id"] == "pf-concentration")
    assert tpl["value"] == "8" and "funds and ETFs not counted" in tpl["why"]
    ftpl = next(t for t in out["templates"] if t["id"] == "pf-fund-concentration")
    assert (ftpl["metric"], ftpl["value"]) == ("max_fund_pct", "25")

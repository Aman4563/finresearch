"""#219: the portfolio data-health panel. Synthetic data only (made-up names, fake ISINs); expected values by hand."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from finresearch.portfolio import health

ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
TODAY = date(2026, 10, 6)  # FY 2026-27; the last completed FY is 2025-26 (2026)


def _lot(hid, acquired, qty, cpu):
    return NS(
        holding_id=hid, acquired=acquired, open_quantity=D(qty), cost_per_unit=None if cpu is None else D(cpu)
    )


def test_purchase_dates_are_value_weighted_and_unweighable_lots_are_said():
    lots = [_lot(1, date(2024, 5, 2), 10, 100), _lot(2, None, 5, 200), _lot(3, None, 1, None)]
    r = health.purchase_dates(lots, {1: 150.0, 2: None, 3: None})
    # dated: 10 x 150 (priced) = 1500; undated: 5 x 200 (cost, unpriced) = 1000; lot 3 has neither
    assert r["coverage_pct"] == 60.0 and r["status"] == "partial"
    assert r["detail"] == ("2 of 3 open lot(s) have no purchase date; 1 lot(s) without a price or cost could not be "
                           "weighed")  # fmt: skip
    assert "XIRR" in r["blocks"] and r["href"] == "/portfolio#import"


def test_priced_by_count_and_pending_prices_are_unknown_not_missing():
    rows = [
        {"closed": False, "value": 10.0},
        {"closed": False, "value": None},
        {"closed": True, "value": None},
    ]
    assert health.priced(rows)["coverage_pct"] == 50.0
    r = health.priced([{"closed": False, "value": None, "pending": True}, {"closed": False, "value": 1.0}])
    assert (r["coverage_pct"], r["status"]) == (None, "unknown")


def test_lookthrough_reads_the_routes_coverage_and_never_turns_an_error_into_complete():
    assert (
        health.lookthrough({"concentration": {"fund_coverage_pct": 72.5}}, True, None)["coverage_pct"] == 72.5
    )
    top = {"coverage": {"pct": 40.0}, "concentration": {"fund_coverage_pct": 72.5}}  # a richer field wins
    assert health.lookthrough(top, True, None)["coverage_pct"] == 40.0
    err = health.lookthrough(None, True, "timed out after 45 s")
    assert (err["status"], err["coverage_pct"]) == ("unknown", None) and "timed out" in err["detail"]
    assert health.lookthrough(None, False, None)["status"] == "not_applicable"


def test_history_against_the_risk_metric_minimums():
    r = health.history({"available": True, "summary": {"days": 61}}, None, True)  # 60 daily returns
    assert r["coverage_pct"] == 50.0  # 60 of the 120 that beta needs
    assert (
        r["detail"]
        == "60 daily returns; too short for beta and risk contribution (120), VaR and Sharpe (250)"
    )
    assert health.history({"available": False, "summary": None}, None, True)["coverage_pct"] == 0.0
    assert health.history(None, "ValueError", True)["status"] == "unknown"


def test_overall_uses_the_stated_weights_drops_rows_that_do_not_apply_and_counts_unknown_as_zero():
    rows = [
        {"label": "a", "weight": 20, "coverage_pct": 60.0, "status": "partial"},
        {"label": "b", "weight": 20, "coverage_pct": None, "status": "unknown"},
        {"label": "c", "weight": 10, "coverage_pct": 100.0, "status": "ok"},
        {"label": "d", "weight": 10, "coverage_pct": None, "status": "not_applicable"},
    ]
    o = health.overall(rows)
    assert o["pct"] == round((20 * 60 + 20 * 0 + 10 * 100) / 50, 1) == 44.0
    assert o["verdict"].startswith("Analysis unreliable") and o["unknown"] == ["b"]
    assert health.overall(rows[:1] + rows[2:3])["verdict"].startswith("Analysis partially reliable")  # 73.3
    assert sum(health.WEIGHTS.values()) == 100


# ----------------------------------------------------------------------------------------------- database + API
TXNS = [
    {"name": "Fakeco Alpha Ltd", "isin": "INE000Z01011", "day": "2023-06-01", "kind": "buy", "quantity": 10,
     "price": 100},
    {"name": "Fakeco Alpha Ltd", "isin": "INE000Z01011", "day": "2024-08-01", "kind": "dividend", "amount": 50},
    {"name": "Imaginary Flexi Cap Fund - Direct Growth", "asset_type": "mf", "day": "2025-06-05", "kind": "buy",
     "quantity": 100, "price": 50},
]  # fmt: skip


@pytest.fixture
def client(env):
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, "
                       "portfolio_import, portfolio_ais, portfolio_setting, wealth_goal, investor_profile"))  # fmt: skip
    with TestClient(create_app()) as c:
        for t in TXNS:
            assert c.post("/api/portfolio/transactions", headers=ORIGIN, json=t).status_code == 201
        yield c
    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, "
                       "portfolio_import, portfolio_ais, portfolio_setting, wealth_goal, investor_profile"))  # fmt: skip


def _snap():
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioHolding

    with session_scope() as s:
        hs = list(s.query(PortfolioHolding).order_by(PortfolioHolding.id))
        rows = [{"id": h.id, "asset_type": h.asset_type, "closed": False, "price": 120.0, "value": 1200.0,
                 "actions_synced": TODAY.isoformat()} for h in hs if h.asset_type == "stock"]  # fmt: skip
        rows += [{"id": h.id, "asset_type": "mf", "closed": False, "price": None, "value": None}
                 for h in hs if h.asset_type == "mf"]  # fmt: skip
    return {"as_of": TODAY.isoformat(), "holdings": rows}


def test_dividend_years_are_completed_fys_with_stock_held(client):
    from finresearch.db import session_scope

    # TestClient only seeds; held from FY 2023-24 (2024) to today: completed FYs 2024, 2025, 2026
    with session_scope() as s:
        r = health.dividends(s, TODAY)
    assert [(x["fy"], x["recorded"]) for x in r["per_fy"]] == [(2024, False), (2025, True), (2026, False)]
    assert r["coverage_pct"] == round(100 / 3, 1) and r["status"] == "partial"


def test_health_api_rows_and_overall(client, monkeypatch):
    from fastapi.routing import APIRoute

    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioAis, WealthGoal

    async def fake_portfolio(prices="live"):
        return _snap()

    def boom(top=25):
        raise RuntimeError("AMC site down")

    async def fake_perf():
        return {"available": True, "summary": {"days": 121}}

    for r in client.app.routes:
        if isinstance(r, APIRoute) and "GET" in r.methods:
            fake = {"/api/portfolio": fake_portfolio, "/api/lookthrough": boom,
                    "/api/portfolio/analytics/performance": fake_perf}.get(r.path)  # fmt: skip
            if fake:
                monkeypatch.setattr(r, "endpoint", fake)
    with session_scope() as s:
        s.add(PortfolioAis(fy=2026, sha256="0" * 64, format="json", items=[], ignored=0, warnings=[]))
        s.add(WealthGoal(name="Synthetic goal", target_inr=D(100000), target_date=date(2030, 1, 1)))
    body = client.get("/api/portfolio/health").json()
    rows = {r["key"]: r for r in body["rows"]}
    assert (
        rows["purchase_dates"]["coverage_pct"] == 100.0
    )  # both lots are dated (one weighed by cost: unpriced)
    assert rows["priced"]["coverage_pct"] == 50.0
    assert rows["dividends"]["coverage_pct"] == round(100 / 3, 1)
    assert (rows["lookthrough"]["status"], rows["lookthrough"]["detail"]) == (
        "unknown", "look-through could not be computed (RuntimeError)")  # fmt: skip
    assert rows["ais"]["coverage_pct"] == 100.0 and rows["ais"]["label"] == "AIS imported for FY 2025-26"
    assert rows["history"]["coverage_pct"] == 100.0
    assert rows["targets"]["coverage_pct"] == 0.0
    assert rows["goals_age"]["coverage_pct"] == 50.0  # a goal, no age
    assert (
        rows["corporate_actions"]["coverage_pct"] == 100.0
    )  # synced today, no unsupported corporate action recorded (#237, #264)
    # purchase dates, priced, corporate actions, dividends, look-through, AIS, history, targets, goals and age
    expected = (
        20 * 100 + 20 * 50 + 10 * 100 + 10 * 100 / 3 + 10 * 0 + 10 * 100 + 10 * 100 + 5 * 0 + 5 * 50
    ) / 100
    assert body["overall"]["pct"] == round(expected, 1) == 65.8
    assert body["overall"]["verdict"] == ("Analysis partially reliable: some figures rest on incomplete data (1 "
                                          "check(s) could not run and count as 0 %)")  # fmt: skip


def test_lookthrough_coverage_from_the_api_coverage_block():
    """/api/lookthrough reports coverage as fund_pct and not_looked_through_pct (% of the portfolio): 47.09 % in funds,
    7.41 % of the portfolio not looked through -> (47.09 - 7.41) / 47.09 = 84.27 % of fund value covered."""
    from finresearch.portfolio.health import lookthrough

    row = lookthrough({"coverage": {"fund_pct": 47.09, "not_looked_through_pct": 7.41}}, True, None)
    assert row["coverage_pct"] == 84.3 and row["status"] != "unknown"
    assert (
        lookthrough({"coverage": {"fund_pct": 0, "not_looked_through_pct": 0}}, True, None)["coverage_pct"]
        is None
    )

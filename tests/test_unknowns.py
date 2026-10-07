"""#238: the remaining paths where an unknown became a number. Synthetic data only (invented names and ISINs).

Each test fails on main @ 7e5d13d and states the hand-computed figure it expects.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

TODAY = date(2026, 10, 5)  # a Monday, FY 2026-27
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}


@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources
    from finresearch.db import session_scope
    from finresearch.fincalc import dates

    async def quote(symbol):
        raise LookupError(symbol)  # no live price for anything: statement prices are the only source

    monkeypatch.setattr(dates, "today_ist", lambda: TODAY)
    with session_scope() as s:
        s.execute(text("TRUNCATE trade_note, alert, portfolio_disposal, portfolio_lot, portfolio_txn, "
                       "portfolio_holding, portfolio_import, portfolio_snapshot, portfolio_setting, investor_profile, "
                       "wealth_asset, wealth_valuation, wealth_loan, wealth_goal, wealth_policy CASCADE"))  # fmt: skip
    app = create_app()
    app.state.markets = MarketSources(quote=quote, today=lambda: TODAY)
    with TestClient(app) as c:
        yield c


def add(c, **body) -> dict:
    base = {"asset_type": "stock", "account": "Demat", "name": "Example Alpha Ltd", "isin": "INE000X01011"}
    r = c.post("/api/portfolio/transactions", headers=ORIGIN, json={**base, **body})
    assert r.status_code == 201, r.text
    return r.json()


def set_meta(txn_id: int | None = None, holding_id: int | None = None, **meta) -> None:
    """Write meta keys only the importers/broker merge write (the API does not take them), then replay the lots."""
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioHolding, PortfolioTxn
    from finresearch.portfolio.service import rebuild

    with session_scope() as s:
        if txn_id is not None:
            t = s.get(PortfolioTxn, txn_id)
            t.meta = {**(t.meta or {}), **meta}
            hid = t.holding_id
        else:
            h = s.get(PortfolioHolding, holding_id)
            h.meta = {**(h.meta or {}), **meta}
            hid = h.id
        s.flush()
        rebuild(s, hid)


def undated_sale(c) -> int:
    """A broker baseline of 10 units at ₹100 (cost known, date unknown) sold in full at ₹150 in this FY:
    gain 10 × 50 = ₹500, term unknown."""
    t = add(c, day="2026-04-10", kind="opening", quantity="10", price="100")
    set_meta(t["id"], statement_opening=True, cost_basis="broker_average")
    add(c, holding_id=t["holding_id"], day="2026-06-01", kind="sell", quantity="10", price="150")
    return t["holding_id"]


# --------------------------------------------------------------------------- (d) advance tax alert
def test_advance_tax_alert_is_unknown_when_the_year_has_an_unclassified_sale(client):
    from finresearch.db import session_scope
    from finresearch.portfolio.metrics import alert_metrics

    undated_sale(client)
    with session_scope() as s:
        m = alert_metrics(s)
    value, why = m["advance_tax_due_inr"][:2]
    # main: Decimal 0 ("below the ₹10,000 threshold") — the ₹500 gain was simply left out of the tax
    assert value is None
    assert "FY 2026-27: unknown" in why and "without an acquisition date" in why
    assert m["ltcg_headroom_inr"][0] is None  # the existing #213 guard, for comparison


def priced_holding(c, day: str = "2026-10-05") -> int:
    """Example Beta: 5 units bought at ₹100, priced only by a broker statement close of ₹120 on `day`."""
    t = add(
        c,
        name="Example Beta Ltd",
        isin="INE000Y01012",
        day="2026-01-05",
        kind="buy",
        quantity="5",
        price="100",
    )
    set_meta(holding_id=t["holding_id"], statement_price={"price": "120", "day": day, "source": "Zerodha"})
    return t["holding_id"]


# --------------------------------------------------------------------------- (c) realised P&L
def test_realised_pnl_counts_sales_whose_cost_is_unknown(client):
    t = add(client, day="2026-04-10", kind="opening", quantity="10")  # a CAS opening balance: cost unknown
    add(client, holding_id=t["holding_id"], day="2026-06-01", kind="sell", quantity="10", price="150")
    priced_holding(client)
    snap = client.get("/api/portfolio").json()
    sm = snap["summary"]
    # Beta: 5 × 120 = ₹600 value, cost ₹500. Alpha: closed, gain unknown (proceeds 10 × 150 = ₹1,500)
    assert sm["value"] == 600.0 and sm["unknown_cost"] == 0  # no OPEN lot has an unknown cost
    assert sm["realised"] == 0.0  # the known part: nothing
    assert sm["realised_unknown"] == 1 and sm["realised_unknown_proceeds"] == 1500.0
    assert "1 sale(s) whose cost is unknown" in sm["realised_note"]
    assert snap["complete"] is False  # main: True
    alpha = next(r for r in snap["holdings"] if r["name"] == "Example Alpha Ltd")
    assert alpha["realised_unknown"] == 1


# --------------------------------------------------------------------------- (b) stale statement prices
def test_statement_price_age_limit_counts_weekdays():
    from finresearch.portfolio.valuation import PriceInfo, statement_stale

    def p(day, source="Zerodha statement close"):
        return PriceInfo(D("120"), day, source)

    # Mon 5-Oct-2026: Mon 28-Sep is 5 weekdays back (29, 30, 1, 2, 5 Oct) -> still current; Fri 25-Sep is 6 -> stale
    assert statement_stale(p("2026-09-28"), TODAY) is None
    why = statement_stale(p("2026-09-25"), TODAY)
    assert why == "Zerodha statement close of 2026-09-25 is 6 trading days old (over 5)"
    assert statement_stale(p(None), TODAY) == "Zerodha statement close with no date"
    assert (
        statement_stale(PriceInfo(D("120"), "2026-01-01", "NSE quote"), TODAY) is None
    )  # live: not this rule
    assert statement_stale(p("2026-08-01", "CAMS statement NAV"), TODAY) is not None  # fund NAVs too


def test_old_statement_price_is_not_complete_and_is_flagged(client):
    hid = priced_holding(client, day="2026-08-06")  # 2 months old
    snap = client.get("/api/portfolio").json()
    (row,) = snap["holdings"]
    assert row["value"] == 600.0 and row["price_stale"] is True  # still shown, with its date
    assert "2026-08-06" in row["price_stale_reason"]
    sm = snap["summary"]
    assert sm["stale"] == 1 and "more than 5 trading days old" in sm["stale_note"]
    assert snap["complete"] is False  # main: True
    # the snapshot records the incompleteness the alerts and net worth read
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioSnapshot

    with session_scope() as s:
        (sn,) = s.query(PortfolioSnapshot).all()
        assert (sn.value, sn.complete) == (D("600.00"), False)
    # data health counts it as not priced
    from finresearch.portfolio.health import priced

    h = priced(snap["holdings"])
    assert h["coverage_pct"] == 0.0 and "1 only by an old statement price" in h["detail"]
    # a fresh one (dated today) is complete
    set_meta(holding_id=hid, statement_price={"price": "120", "day": "2026-10-05", "source": "Zerodha"})
    snap = client.get("/api/portfolio").json()
    assert snap["complete"] is True and snap["summary"]["stale"] == 0


def test_xirr_reason_names_the_stale_price(client):
    priced_holding(client, day="2026-08-06")  # bought 5-Jan-2026 (> 60 days): the XIRR is computed
    snap = client.get("/api/portfolio").json()
    (row,) = snap["holdings"]
    assert row["xirr"] is not None
    assert row["xirr_reason"].startswith(
        "today's value uses an old price: Zerodha statement close of 2026-08-06"
    )
    assert snap["summary"]["xirr"] is not None
    assert (
        "1 holding(s) valued at a statement price more than 5 trading days old"
        in snap["summary"]["xirr_reason"]
    )


# --------------------------------------------------------------------------- (a) net worth without a valuation
def test_net_worth_portfolio_is_unknown_without_a_valuation(client):
    from finresearch.db import session_scope
    from finresearch.wealth.service import load, net_worth_on

    add(client, day="2026-01-05", kind="buy", quantity="5", price="100")  # held, never valued
    r = client.post("/api/wealth/assets", headers=ORIGIN, json={"kind": "cash", "name": "Savings",
                                                                 "value": "300000", "value_date": "2026-09-01"})  # fmt: skip
    assert r.status_code == 200, r.text
    with session_scope() as s:
        nw = net_worth_on(load(s), TODAY)
    # main: portfolio 0.0 and nothing said; the cash ₹3,00,000 is the only known part
    assert nw["portfolio"] is None and nw["portfolio_day"] is None
    assert nw["complete"] is False and "no valuation on or before this day" in nw["missing"][0]
    assert nw["net_worth"] == 300_000.0  # the known parts only, flagged by `complete`


def test_net_worth_says_when_the_valuation_was_incomplete(client):
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioSnapshot
    from finresearch.wealth.service import load, net_worth_on

    add(client, day="2026-01-05", kind="buy", quantity="5", price="100")
    with session_scope() as s:
        s.add(
            PortfolioSnapshot(
                day=date(2026, 10, 2), value=D(600), invested=D(500), by_asset={}, complete=False
            )
        )
    with session_scope() as s:
        nw = net_worth_on(load(s), TODAY)
    assert nw["portfolio"] == 600.0 and nw["complete"] is False
    assert "valuation of 2026-10-02 is incomplete" in nw["missing"][0]


def test_net_worth_without_any_holdings_is_complete_at_zero(client):
    from finresearch.db import session_scope
    from finresearch.wealth.service import load, net_worth_on

    with session_scope() as s:
        nw = net_worth_on(load(s), TODAY)
    assert nw["portfolio"] == 0.0 and nw["complete"] is True and nw["missing"] == []


# --------------------------------------------------------------------------- (e) switch cost and churn cost
def _unknown_gain(sold: date):
    from finresearch.fincalc.tax import Gain, classify

    return Gain(D(500), classify("equity", None, sold), sold)  # a sale this year without a purchase date


def test_switch_cost_is_an_estimate_when_the_year_has_an_unclassified_sale():
    from finresearch.portfolio.analytics import FundLots, _switch_text, switch_cost
    from finresearch.portfolio.tax import HoldingTax

    ht = HoldingTax(3, "Test Flexi Cap Fund", "Test", None, "equity", False, None)
    fl = [FundLots(3, ht, [(date(2025, 1, 6), D(5000), D(10))])]
    today = date(2026, 1, 8)
    ok = switch_cost(fl, 20.0, today, [], D("0.30"), None)
    assert ok["complete"] is True and ok["estimate"] is False and ok["year_unclassified"] == 0
    sw = switch_cost(fl, 20.0, today, [_unknown_gain(date(2025, 12, 1))], D("0.30"), None)
    # main: the same "tax" with no flag; the ₹500 of unclassified gain is simply not in the year's tax
    assert sw["complete"] is False and sw["estimate"] is True and sw["year_unclassified"] == 1
    text = _switch_text("Test Flexi Cap Fund", sw, 1000.0, None)
    assert "(estimate, incomplete)" in text and "left out of the year's tax" in text


def test_churn_cost_is_an_estimate_when_a_year_has_an_unclassified_sale(client):
    from finresearch.portfolio.history import History

    undated_sale(client)  # sold 1-Jun-2026, purchase date unknown
    a = add(client, name="Example Gamma Ltd", isin="INE000Z01013", day="2026-05-04", kind="buy", quantity="10",
            price="100")  # fmt: skip
    add(client, holding_id=a["holding_id"], day="2026-06-02", kind="sell", quantity="10", price="120")

    async def no_history():
        return History(reason="offline test"), TODAY, "fp"

    client.app.state.journal_today = TODAY
    client.app.state.portfolio_history = no_history
    r = client.get("/api/portfolio/behaviour")
    assert r.status_code == 200, r.text
    ch = r.json()["churn"]
    # Gamma: STCG 200 at 20 % = 40 + cess 1.60 = 41.60 (the classified part); Alpha's ₹500 cannot be classified
    assert ch["tax"] == pytest.approx(41.60, abs=0.005)
    assert ch["complete"] is False and ch["estimate"] is True  # main: no flag at all
    assert "1 disposal(s) in FY 2026-27" in ch["incomplete_note"]
    assert ch["by_fy"] == [{"fy": 2027, "tax": pytest.approx(41.6), "tax_short_term": pytest.approx(41.6),
                            "complete": False, "unclassified": 1}]  # fmt: skip

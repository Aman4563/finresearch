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

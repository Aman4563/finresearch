"""Unknown holding period never shows as ₹0 tax (#213) and tax rules past their verified year are flagged (#220).

Synthetic data only (Example names, fake ISINs). Every expected value is hand-computed in the comment next to it.
Rates (FY 2026-27, Income-tax Act 2025 s.196 / s.198): equity STCG 20 %, LTCG 12.5 % above ₹1.25 lakh, cess 4 %.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.tax import VERIFIED_THROUGH_FY, Gain, classify, fy_tax, rules_verified
from finresearch.portfolio.rebalance import Lot, Position, plan
from finresearch.portfolio.tax import (
    DisposalRow,
    HoldingTax,
    OpenLot,
    evaluate,
    export_csv,
    fy_summary,
    harvest,
    unclassified,
)

TODAY = date(2026, 10, 6)  # FY 2026-27
SLAB = D("0.30")
EQ = HoldingTax(1, "Example Alpha Ltd", "Demat", "INE000X01011", "equity", True, None)


def sale(acquired, sold, qty, cost, proceeds, h=EQ):
    return evaluate(
        DisposalRow(h, acquired, sold, D(qty), None if cost is None else D(cost), D(proceeds), True, "buy")
    )


def year_rows():
    """FY 2026-27: a dated short-term sale and an undated one (a broker baseline: cost known, date not).
    - dated: bought 1-Jun-2026, sold 1-Sep-2026 (3 months, short), 10 units, cost 1,000, proceeds 2,000: gain 1,000
      -> 20 % = 200, cess 8 -> 208
    - undated: 10 units, cost 1,202, proceeds 2,000 -> gain 798, term unknown (left out of the tax)"""
    return [
        sale(date(2026, 6, 1), date(2026, 9, 1), 10, 1000, 2000),
        sale(None, date(2026, 9, 2), 10, 1202, 2000),
    ]


# --------------------------------------------------------------------------- #213
def test_undated_lot_with_known_cost_is_unclassified_not_zero():
    r = sale(None, date(2026, 9, 2), 10, 1202, 2000)
    assert r.gain == D(798) and r.cls.term == "unknown"  # the gain is known, its term is not
    assert any("Term unknown" in n for n in r.notes)  # the CSV notes column says why
    u = unclassified([r])
    assert (u["count"], u["gain"], u["no_date"], u["no_cost"]) == (1, 798.0, 1, 0)
    assert "1 disposal(s) / ₹798 of gains can't be classified" in u["detail"]


def test_fy_summary_with_a_dated_and_an_undated_disposal_is_incomplete():
    s = fy_summary(year_rows(), 2027, SLAB)
    assert s["complete"] is False and s["unknown"] == 1
    assert (s["tax"], s["cess"], s["total"]) == (
        None,
        None,
        None,
    )  # never the partial figure as the year's tax
    assert s["total_classified"] == 208.0  # 1,000 x 20 % = 200 + 4 % cess = 208
    assert s["unclassified"]["gain"] == 798.0 and s["exemption"]["complete"] is False
    assert s["stcg"] == 1000.0  # the undated gain is not counted as short-term either


def test_fy_summary_complete_without_unknowns():
    s = fy_summary(year_rows()[:1], 2027, SLAB)
    assert s["complete"] is True and s["total"] == 208.0 and s["unclassified"]["count"] == 0


def test_cost_unknown_counts_too_and_intraday_does_not():
    no_cost = sale(date(2025, 1, 1), date(2026, 9, 3), 5, None, 900)
    intra = evaluate(
        DisposalRow(EQ, date(2026, 9, 4), date(2026, 9, 4), D(1), D(100), D(110), True, "intraday")
    )
    u = unclassified([no_cost, intra])
    assert (u["count"], u["no_cost"], u["gain"]) == (1, 1, 0.0)  # intraday is business income, not "unknown"
    assert "the gain of the 1 without a cost is unknown too" in u["detail"]


def test_harvest_suggests_no_tax_free_gain_while_the_year_is_incomplete():
    # an open long-term lot: bought 1-Jan-2024 at ₹100, price ₹150, 100 units -> gain 5,000 (inside ₹1.25 lakh)
    lots = {1: [OpenLot(EQ, date(2024, 1, 1), D(100), D(100))]}
    ok = harvest(year_rows()[:1], lots, {1: D(150)}, {1: "stock"}, TODAY, SLAB)
    assert ok["complete"] is True and ok["gain_harvest"][0]["gain"] == 5000.0
    assert ok["exemption_remaining"] == 125000.0 and ok["tax_so_far"] == 208.0
    got = harvest(year_rows(), lots, {1: D(150)}, {1: "stock"}, TODAY, SLAB)
    assert got["complete"] is False and got["gain_harvest"] == []
    assert got["exemption_remaining"] is None and got["tax_so_far"] is None
    assert "can't be classified" in got["notes"][0]


def test_csv_header_says_the_year_is_incomplete():
    text = export_csv(year_rows(), 2027)
    assert "# FY 2026-27: Tax incomplete: 1 disposal(s) / ₹798 of gains" in text
    assert "Term unknown" in text  # the row's notes column


def test_rebalance_tax_is_an_estimate_while_the_year_is_incomplete():
    # stocks 60k (600 @ ₹100, cost ₹50, bought 1-Jan-2025: long) / debt 40k against 50/50: sell ₹10k of stock
    ht = HoldingTax(2, "Example Beta Ltd", "Demat", "INE000X02019", "equity", True, None)
    st = Position(ht, "stock", "Stocks", D(60000), D(100), [Lot(date(2025, 1, 1), D(600), D(50))])
    dh = HoldingTax(3, "Example Debt Fund", "Folio", None, "debt_mf", False, None)
    dt = Position(dh, "mf", "Debt funds", D(40000), D(10), [Lot(date(2024, 1, 1), D(4000), D(10))])
    full = plan([st, dt], {"Stocks": 50, "Debt funds": 50}, TODAY, year_rows()[:1], SLAB)
    assert (
        full["tax_complete"] is True and full["totals"]["tax_so_far"] == 208.0 and full["tax_warnings"] == []
    )
    out = plan([st, dt], {"Stocks": 50, "Debt funds": 50}, TODAY, year_rows(), SLAB)
    t = out["totals"]
    assert out["tax_complete"] is False and t["tax_estimate"] is True
    assert (t["tax_so_far"], t["exemption_before"], t["exemption_after"]) == (None, None, None)
    assert "can't be classified" in out["tax_warnings"][0]


# --------------------------------------------------------------------------- #220
def test_verified_through_is_fy_2026_27():
    assert VERIFIED_THROUGH_FY == 2027 == fiscal_year(TODAY)  # 6-Oct-2026 is in FY 2026-27
    assert rules_verified(2027) and not rules_verified(2028)


def test_sale_after_the_verified_year_is_flagged_not_silently_applied():
    last, first = date(2027, 3, 31), date(2027, 4, 1)  # last day of FY 2026-27, first of FY 2027-28
    for d in (last, first):  # the open-ended rows still apply ...
        assert classify("equity", date(2026, 1, 1), d).rule.id == "equity-2026"
    a = fy_tax([Gain(D(1000), classify("equity", date(2026, 6, 1), last), last)], 2027, SLAB)
    b = fy_tax([Gain(D(1000), classify("equity", date(2026, 6, 1), first), first)], 2028, SLAB)
    assert a.rules_verified and not any("not verified" in n for n in a.notes)
    assert not b.rules_verified and any(
        "Rules not verified for FY 2027-28" in n for n in b.notes
    )  # ... flagged
    rows = [sale(date(2026, 6, 1), first, 10, 1000, 2000)]
    s = fy_summary(rows, 2028, SLAB)
    assert s["rules_verified"] is False and "FY 2027-28" in s["rules_note"]
    assert s["total"] == 208.0  # still computed (same rates assumed), but marked
    assert "# Rules not verified for FY 2027-28" in export_csv(rows, 2028)
    h = harvest(rows, {}, {}, {}, date(2027, 4, 5), SLAB)
    assert h["rules_verified"] is False and "FY 2027-28" in h["notes"][0]
    assert fy_summary(rows[:0], 2027, SLAB)["rules_verified"] is True


def test_rebalance_after_the_verified_year_is_flagged():
    ht = HoldingTax(2, "Example Beta Ltd", "Demat", "INE000X02019", "equity", True, None)
    st = Position(ht, "stock", "Stocks", D(60000), D(100), [Lot(date(2025, 1, 1), D(600), D(50))])
    dh = HoldingTax(3, "Example Debt Fund", "Folio", None, "debt_mf", False, None)
    dt = Position(dh, "mf", "Debt funds", D(40000), D(10), [Lot(date(2024, 1, 1), D(4000), D(10))])
    out = plan([st, dt], {"Stocks": 50, "Debt funds": 50}, date(2027, 4, 5), [], SLAB)
    assert out["rules_verified"] is False and any("FY 2027-28" in w for w in out["tax_warnings"])


# --------------------------------------------------------------------------- the checklist, end to end
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources
    from finresearch.db import session_scope

    async def quote(symbol):
        raise LookupError(symbol)

    async def no_signal(asset, code):
        raise LookupError("offline test")

    with session_scope() as s:
        s.execute(text("TRUNCATE trade_note, alert, portfolio_disposal, portfolio_lot, portfolio_txn, "
                       "portfolio_holding, portfolio_import, portfolio_snapshot, portfolio_setting, investor_profile "
                       "CASCADE"))  # fmt: skip
    app = create_app()
    app.state.markets = MarketSources(quote=quote, today=lambda: TODAY)
    app.state.journal_today = TODAY
    app.state.journal_signal = no_signal
    with TestClient(app) as c:
        yield c


def _setup(c) -> int:
    """A broker baseline of 10 units at ₹100 (cost known, purchase date unknown, as connectors.merge writes it) and a
    dated buy of 10 at ₹100 on 10-Jan-2025."""
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioTxn
    from finresearch.portfolio.service import rebuild

    r = c.post("/api/portfolio/transactions", headers=ORIGIN, json={
        "asset_type": "stock", "account": "Demat", "name": "Example Alpha Ltd", "isin": "INE000X01011",
        "day": "2025-01-01", "kind": "opening", "quantity": "10", "price": "100"})  # fmt: skip
    assert r.status_code == 201, r.text
    hid = r.json()["holding_id"]
    with session_scope() as s:  # the API does not take this key: only the broker merge writes it
        t = s.get(PortfolioTxn, r.json()["id"])
        t.meta = {"statement_opening": True, "cost_basis": "broker_average"}
        s.flush()
        rebuild(s, hid)
    r = c.post("/api/portfolio/transactions", headers=ORIGIN, json={
        "asset_type": "stock", "account": "Demat", "holding_id": hid, "day": "2025-01-10", "kind": "buy",
        "quantity": "10", "price": "100"})  # fmt: skip
    assert r.status_code == 201, r.text
    return hid


def _tax(c, hid, qty, day=None):
    body = {
        "side": "sell",
        "holding_id": hid,
        "quantity": qty,
        "price": "150",
        **({"day": day} if day else {}),
    }
    out = c.post("/api/journal/pretrade", headers=ORIGIN, json=body).json()
    return {i["key"]: i for i in out["items"]}["tax"], out


def test_checklist_tax_is_unknown_not_zero_for_an_undated_lot(client):
    hid = _setup(client)
    # sell 15 @ ₹150: FIFO takes the undated 10 first (gain 10 x 50 = ₹500, term unknown), then 5 dated units
    # (bought 10-Jan-2025, long-term on 6-Oct-2026: gain ₹250, inside the ₹1.25 lakh exemption -> ₹0 tax)
    tax, out = _tax(client, hid, "15")
    assert tax["status"] == "unknown" and tax["value"] is None  # before #213: status "ok", value 0
    assert tax["unclassified"]["count"] == 1 and tax["unclassified"]["gain"] == 500.0
    assert (
        "₹500 of gain) can't be classified" in tax["detail"]
        and "without an acquisition date" in tax["detail"]
    )
    assert tax["classified_tax"] == 0.0 and out["status"] != "ok"


def test_checklist_flags_unverified_rules_for_a_later_year(client):
    hid = _setup(client)
    tax, _ = _tax(client, hid, "15", "2027-04-02")  # FY 2027-28: past VERIFIED_THROUGH_FY
    assert tax["rules_verified"] is False and "Rules not verified for FY 2027-28" in tax["detail"]

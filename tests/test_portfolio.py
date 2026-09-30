"""Portfolio import, lots, valuation and tax through the API, on synthetic data only.

The CAS PDF is generated at test time (tests/cas_synth.py) and parsed by the real casparser; tradebooks are small
invented CSV/XLSX files. Quotes and AMFI NAVs are fakes, so nothing touches the network.
"""

from __future__ import annotations

import base64
import io
from datetime import date
from decimal import Decimal as D

import pytest
from cas_synth import build_cas, sample_statement
from fastapi.testclient import TestClient

from finresearch.adapters.amfi import SchemeNav
from finresearch.adapters.nse import Quote
from finresearch.adapters.nse_equity import CorporateAction
from finresearch.api.markets import MarketSources
from finresearch.portfolio.importers import (
    StatementError,
    detect_broker,
    parse_cas,
    parse_tradebook,
    read_table,
)

PASSWORD = "ABCDE1234F"  # invented, PAN-shaped
TODAY = date(2026, 9, 30)
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}

ZERODHA = """symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time
EXMPL,INE000A01011,2024-01-10,NSE,EQ,EQ,buy,false,10.000000,100.000000,1001,5001,2024-01-10T10:00:00
EXMPL,INE000A01011,2024-06-10,NSE,EQ,EQ,buy,false,10.000000,150.000000,1002,5002,2024-06-10T10:00:00
EXMPL,INE000A01011,2024-07-22,NSE,EQ,EQ,sell,false,5.000000,200.000000,1003,5003,2024-07-22T10:00:00
EXMPL,INE000A01011,2024-07-23,NSE,EQ,EQ,sell,false,5.000000,200.000000,1004,5004,2024-07-23T10:00:00
NIFTY26SEPFUT,,2026-09-01,NFO,FO,,buy,false,75,25000,1005,5005,2026-09-01T10:00:00
"""
UPSTOX = """Date,Company,Amount,Exchange,Segment,Scrip Code,Instrument Type,Strike Price,Expiry,Trade Num,Trade Time,Side,Quantity,Price
12-02-2025,EXAMPLE TEXTILES LTD,1000,NSE,EQ,11111,EQ,,,T1,10:15:00,Buy,10,100
12-03-2025,EXAMPLE TEXTILES LTD,1100,NSE,EQ,11111,EQ,,,T2,10:15:00,Sell,5,220
12-03-2025,NIFTY,50,NSE,FO,22222,OPTIDX,25000,26-03-2025,T3,10:16:00,Buy,75,50
"""


def groww_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in (
        ["Name", "Synthetic Investor"],
        ["Unique Client Code", "0000"],
        [],
        ["Stock Order History"],
        [],
    ):
        ws.append(row)
    ws.append(["Stock name", "Symbol", "ISIN", "Type", "Quantity", "Value", "Exchange", "Exchange Order Id",
               "Execution date and time", "Order status"])  # fmt: skip
    ws.append(["Example Bank Ltd", "EXBANK", "INE000B01012", "BUY", 4, 2000, "NSE", "X1", "05-01-2026 10:30 AM",
               "Executed"])  # fmt: skip
    ws.append(["Example Bank Ltd", "EXBANK", "INE000B01012", "BUY", 1, 520, "NSE", "X2", "06-01-2026 10:30 AM",
               "Cancelled"])  # fmt: skip
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- importers (no DB)
def test_cas_parsed_by_casparser_and_identity_dropped():
    res = parse_cas(build_cas(sample_statement()), PASSWORD)
    assert (res.kind, res.source, res.period) == ("cas", "CAMS", ("01-Apr-2025", "29-Sep-2026"))
    kinds = [(t.name.split(" - ")[0], t.kind, str(t.quantity or t.amount)) for t in res.txns]
    assert ("Example Flexi Cap Fund", "opening", "50.000") in kinds
    assert ("Example Flexi Cap Fund", "dividend", "1200.00") in kinds  # reinvested IDCW: income + units
    buy = next(t for t in res.txns if t.kind == "buy" and t.day == date(2025, 5, 10))
    assert buy.charges == D("0.50")  # the same-day stamp-duty row joins the purchase's cost
    assert [c.units for c in res.closing] == [D("150.000"), D("300.000")]
    blob = repr(res)
    assert "ABCDE1234F" not in blob and "synthetic.investor" not in blob and "Synthetic Investor" not in blob


def test_cas_wrong_password_message_is_safe():
    with pytest.raises(StatementError) as e:
        parse_cas(build_cas(sample_statement()), "WRONG-SECRET-999")
    assert "wrong password" in str(e.value) and "WRONG-SECRET-999" not in str(e.value)


def test_tradebook_detection_by_headers():
    z = parse_tradebook(ZERODHA.encode(), "tradebook.csv")
    assert z.source == "zerodha" and len(z.txns) == 4 and sum(z.skipped.values()) == 1  # the F&O row
    g = parse_tradebook(groww_xlsx(), "orders.xlsx")
    assert g.source == "groww" and len(g.txns) == 1 and g.txns[0].price == D(500)  # Value 2000 / 4
    u = parse_tradebook(UPSTOX.encode(), "trades.csv")
    assert u.source == "upstox" and [(t.kind, t.quantity) for t in u.txns] == [("buy", D(10)), ("sell", D(5))]
    assert u.txns[0].day == date(2025, 2, 12) and u.txns[0].nse_symbol is None  # a name, not a symbol
    assert detect_broker(read_table(b"a,b,c\n1,2,3\n")) is None
    with pytest.raises(StatementError):
        parse_tradebook(b"a,b,c\n1,2,3\n")


# --------------------------------------------------------------------------- API
def quote_for(symbol: str) -> Quote:
    prices = {"EXMPL": D(250), "EXBANK": D(480)}
    if symbol not in prices:
        raise LookupError(symbol)
    return Quote(symbol=symbol, last_price=prices[symbol], issued_shares=D(5 * 10**9) if symbol == "EXMPL" else D(10**8),
                 industry="Textiles" if symbol == "EXMPL" else "Banks")  # fmt: skip


class FakeEquity:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def corporate_actions(self, symbol):
        rows = [{"symbol": symbol, "subject": "Bonus 1:1", "exDate": "15-Mar-2024", "recDate": "15-Mar-2024"},
                {"symbol": symbol, "subject": "Bonus 1:1", "exDate": "15-Mar-2024", "recDate": "15-Mar-2024"},
                {"symbol": symbol, "subject": "Dividend - Rs 5 Per Share", "exDate": "01-Aug-2024", "recDate": ""},
                {"symbol": symbol, "subject": "Bonus 1:1", "exDate": "01-Jan-2020", "recDate": ""}]  # fmt: skip
        return [CorporateAction.parse(r) for r in rows]


@pytest.fixture
def client(env):
    from finresearch.api import create_app

    async def nav_all():
        return [SchemeNav("100001", "Example Short Duration Fund - Direct Plan - Growth", "Direct", "Growth",
                          None, None, D("106"), date(2026, 9, 29), "Debt Scheme - Short Duration Fund", "Example MF")]  # fmt: skip

    async def quote(symbol):
        return quote_for(symbol)

    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:  # the test database is shared by the session: start every test empty
        s.execute(
            text(
                "TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                "portfolio_snapshot, portfolio_setting"
            )
        )
    app = create_app(nav_all=nav_all)
    app.state.markets = MarketSources(quote=quote, equity=lambda: FakeEquity(), today=lambda: TODAY)
    with TestClient(app) as c:
        yield c


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def post_cas(c, pdf: bytes, *, dry: bool, password: str = PASSWORD, name: str = "cas.pdf"):
    return c.post("/api/portfolio/import/cas", headers=ORIGIN,
                  json={"filename": name, "content_b64": b64(pdf), "password": password, "dry_run": dry})  # fmt: skip


def test_cas_import_reconciles_units_and_is_idempotent(client):
    pdf = build_cas(sample_statement())
    prev = post_cas(client, pdf, dry=True).json()
    assert prev["dry_run"] and prev["new_rows"] == 8 and prev["reconciled"], prev
    assert client.get("/api/portfolio").json()["holdings"] == []  # a dry run writes nothing

    done = post_cas(client, pdf, dry=False).json()
    assert done["added"] == 8 and done["reconciled"]
    # reconciliation: the statement's closing units equal the units in the lots
    rec = {r["name"].split(" - ")[0]: (r["statement_units"], r["lot_units"]) for r in done["reconciliation"]}
    assert rec == {
        "Example Flexi Cap Fund": ("150.000", "150.000"),
        "Example Short Duration Fund": ("300.000", "300.000"),
    }
    assert post_cas(client, pdf, dry=False).status_code == 409  # the same file twice

    # an overlapping statement (earlier start, same rows): nothing new, units unchanged
    older = sample_statement(start="01-Jan-2025")
    again = post_cas(client, build_cas(older), dry=False, name="cas2.pdf").json()
    assert again["added"] == 1 and again["duplicates"] == 7 and again["reconciled"], (
        again
    )  # its own opening row
    snap = client.get("/api/portfolio").json()
    units = {h["name"].split(" - ")[0]: h["units"] for h in snap["holdings"]}
    assert units == {"Example Flexi Cap Fund": 150.0, "Example Short Duration Fund": 300.0}

    # the saved file sits under the gitignored portfolio dir, still encrypted; no password anywhere in the DB
    from sqlalchemy import text

    from finresearch.config import get_settings
    from finresearch.db import session_scope

    saved = list((get_settings().portfolio_dir / "imports").rglob("*.pdf"))
    assert len(saved) == 2 and all(p.read_bytes() != b"" for p in saved)
    with session_scope() as s:
        dump = " ".join(str(r) for r in s.execute(text(
            "SELECT i.summary::text || coalesce(i.filename,'') FROM portfolio_import i UNION ALL "
            "SELECT meta::text FROM portfolio_txn UNION ALL SELECT meta::text || name || account FROM portfolio_holding")))  # fmt: skip
    assert PASSWORD not in dump and "synthetic.investor" not in dump


def test_password_never_echoed(client):
    secret = "SENTINEL-PASS-4242"
    bad_b64 = client.post("/api/portfolio/import/cas", headers=ORIGIN,
                          json={"content_b64": "@@not-base64@@", "password": secret})  # fmt: skip
    wrong = post_cas(client, build_cas(sample_statement()), dry=True, password=secret)
    not_obj = client.post("/api/portfolio/import/cas", headers=ORIGIN, content=b"[1, 2]")
    for r in (bad_b64, wrong, not_obj):
        assert r.status_code == 422 and secret not in r.text, r.text
    assert "wrong password" in wrong.json()["detail"]
    # the CSRF guard still applies to the upload route
    assert (
        client.post(
            "/api/portfolio/import/cas", headers={"Origin": "https://evil.example"}, json={}
        ).status_code
        == 403
    )


def test_tradebook_import_valuation_xirr_and_tax(client):
    r = client.post("/api/portfolio/import/tradebook", headers=ORIGIN,
                    json={"filename": "tb.csv", "content_b64": b64(ZERODHA.encode()), "dry_run": False})  # fmt: skip
    assert r.status_code == 200 and r.json()["added"] == 4
    again = client.post("/api/portfolio/import/tradebook", headers=ORIGIN,
                        json={"filename": "tb2.csv", "content_b64": b64(ZERODHA.encode() + b"\n"), "dry_run": False})  # fmt: skip
    assert again.json()["added"] == 0 and again.json()["duplicates"] == 4  # same trade ids in another file

    snap = client.get("/api/portfolio").json()
    (h,) = snap["holdings"]
    # FIFO: the two sales (5 + 5) consume the 10 bought at 100; 10 @150 remain, worth 10 × 250
    assert (h["units"], h["cost"], h["value"], h["unrealised"]) == (10.0, 1500.0, 2500.0, 1000.0)
    assert h["realised"] == 1000.0 and h["tax_class"] == "equity" and h["sector"] == "Textiles"
    assert (
        h["cap_bucket"] == "Large cap"
    )  # 250 × 5e9 shares = ₹1,25,000 crore, above the ₹1,05,174 crore cut-off
    assert h["signal"] == {"asset": "stock", "instrument": "EXMPL", "href": "/stocks/EXMPL"}
    assert h["xirr"] is not None and snap["summary"]["value"] == 2500.0


def test_tax_boundary_and_export(client):
    client.post("/api/portfolio/import/tradebook", headers=ORIGIN,
                json={"filename": "tb.csv", "content_b64": b64(ZERODHA.encode()), "dry_run": False})  # fmt: skip
    tax = client.get("/api/portfolio/tax").json()
    d = {x["sold"]: x for x in tax["disposals"]}
    # both sales are short-term (bought 10-Jan-2024): 22-Jul-2024 at 15 %, 23-Jul-2024 at 20 %
    assert (d["2024-07-22"]["rate_pct"], d["2024-07-22"]["rule"]) == (15.0, "equity-2018")
    assert (d["2024-07-23"]["rate_pct"], d["2024-07-23"]["rule"]) == (20.0, "equity-2024")
    fy25 = next(f for f in tax["fys"] if f["fy"] == 2025)
    assert fy25["stcg"] == 1000.0 and fy25["tax"] == 175.0  # 500 × 15 % + 500 × 20 %
    assert tax["verify"].startswith("Personal estimate") and tax["rules"]
    csv_text = client.get("/api/portfolio/tax.csv?fy=2025").text
    assert "cost_for_tax" in csv_text and "equity-2024" in csv_text and csv_text.count("\n") == 5


def test_manual_entry_bonus_sync_and_delete(client):
    body = {"asset_type": "stock", "name": "Example Textiles", "nse_symbol": "exmpl", "account": "Manual",
            "day": "2023-01-02", "kind": "buy", "quantity": "10", "price": "100"}  # fmt: skip
    r = client.post("/api/portfolio/transactions", headers=ORIGIN, json=body)
    assert r.status_code == 201
    hid = r.json()["holding_id"]
    sync = client.post("/api/portfolio/actions/sync", headers=ORIGIN).json()
    # the 2020 bonus predates the purchase and the 2024 bonus is listed twice: one bonus event is added
    assert sync["added"] == {"Example Textiles": ["bonus 2024-03-15"]}
    assert client.post("/api/portfolio/actions/sync", headers=ORIGIN).json()["added"] == {}
    detail = client.get(f"/api/portfolio/holdings/{hid}").json()
    assert [(x["origin"], x["open_quantity"], x["cost_per_unit"]) for x in detail["lots"]] == [
        ("buy", "10", "100"), ("bonus", "10", "0")]  # fmt: skip
    assert client.post("/api/portfolio/transactions", headers=ORIGIN,
                       json={**body, "kind": "split"}).status_code == 422  # a split needs from/to  # fmt: skip
    # overrides: tax class and FMV
    assert client.put(f"/api/portfolio/holdings/{hid}", headers=ORIGIN,
                      json={"tax_class": "other", "fmv_2018": "90"}).status_code == 200  # fmt: skip
    h = next(x for x in client.get("/api/portfolio").json()["holdings"] if x["id"] == hid)
    assert (h["tax_class"], h["tax_class_auto"], h["fmv_2018"]) == ("other", "equity", 90.0)
    for t in detail["transactions"]:
        assert client.delete(f"/api/portfolio/transactions/{t['id']}", headers=ORIGIN).status_code == 200
    assert client.get("/api/portfolio").json()["holdings"] == []


def test_delete_import_removes_its_rows(client):
    pdf = build_cas(sample_statement())
    imp = post_cas(client, pdf, dry=False).json()["import_id"]
    assert len(client.get("/api/portfolio/imports").json()) == 1
    assert client.delete(f"/api/portfolio/imports/{imp}", headers=ORIGIN).status_code == 200
    assert client.get("/api/portfolio").json()["holdings"] == []
    from finresearch.config import get_settings

    assert not list((get_settings().portfolio_dir / "imports").rglob("*.pdf"))


# --------------------------------------------------------------------------- tax class and harvesting (pure)
@pytest.mark.parametrize(
    ("asset", "name", "category", "symbol", "expected"),
    [
        ("mf", "Example Flexi Cap Fund - Direct - Growth", "Equity Scheme - Flexi Cap Fund", None, "equity"),
        ("mf", "Example Arbitrage Fund", "Hybrid Scheme - Arbitrage Fund", None, "equity"),
        ("mf", "Example Liquid Fund", "Debt Scheme - Liquid Fund", None, "debt_mf"),
        ("mf", "Example Gold ETF Fund of Fund", "Other Scheme - FoF Domestic", None, "other_mf"),
        ("mf", "Example Nifty 50 Index Fund", "Other Scheme - Index Funds", None, "equity"),
        ("mf", "Example Nifty SDL Apr 2027 Index Fund", "Other Scheme - Index Funds", None, "debt_mf"),
        ("stock", "SGB 2.50% 2029", None, "SGBMAR29", "sgb"),
        ("stock", "GOLDBEES", None, "GOLDBEES", "other_mf"),
        ("stock", "Example Industries", None, "EXIND", "equity"),
    ],
)
def test_auto_tax_class(asset, name, category, symbol, expected):
    from finresearch.portfolio.tax import auto_tax_class

    assert auto_tax_class(asset, name, category, None, symbol)[0] == expected


def test_harvest_gain_up_to_exemption_and_loss_with_tax_saving():
    from finresearch.portfolio.tax import DisposalRow, HoldingTax, OpenLot, evaluate, harvest

    eq = HoldingTax(1, "Winner", "Zerodha", None, "equity", True, None)
    loser = HoldingTax(2, "Loser", "Zerodha", None, "equity", True, None)
    today = date(2026, 1, 15)  # FY 2025-26
    # already realised this year: STCG 50,000 (20 %) -> tax 10,000 + cess
    realised = [evaluate(DisposalRow(eq, date(2025, 6, 1), date(2025, 9, 1), D(100), D(100_000), D(150_000), True,
                                     "buy"))]  # fmt: skip
    lots = {1: [OpenLot(eq, date(2023, 1, 1), D(1000), D(100))],  # LT gain 900 per unit at price 1,000
            2: [OpenLot(loser, date(2025, 10, 1), D(100), D(500))]}  # ST loss 200 per unit at price 300  # fmt: skip
    out = harvest(realised, lots, {1: D(1000), 2: D(300)}, {1: "stock", 2: "stock"}, today, D("0.30"))
    (g,) = out["gain_harvest"]
    # headroom 1,25,000 / 900 per unit = 138 units -> gain 1,24,200
    assert (g["sell_units"], g["gain"]) == (138.0, 124200.0)
    (lo,) = out["loss_harvest"]
    # selling all 100 loses 20,000, which wipes out the 50,000 STCG's tax on 20,000: 4,000 + 4 % cess = 4,160
    assert (lo["sell_units"], lo["loss"], lo["tax_saved"]) == (100.0, -20000.0, 4160.0)
    assert out["days_left"] == 75 and any("wash-sale" in n for n in out["notes"])


def test_manual_entry_joins_the_tradebook_holding(client):
    client.post("/api/portfolio/import/tradebook", headers=ORIGIN,
                json={"filename": "tb.csv", "content_b64": b64(ZERODHA.encode()), "dry_run": False})  # fmt: skip
    (h,) = client.get("/api/portfolio").json()["holdings"]
    r = client.post("/api/portfolio/transactions", headers=ORIGIN,
                    json={"asset_type": "stock", "name": "Example IPO allotment", "nse_symbol": "EXMPL", "account": "Zerodha",
                          "day": "2025-01-02", "kind": "buy", "quantity": "5", "price": "90"})  # fmt: skip
    assert r.json()["holding_id"] == h["id"]
    (h2,) = client.get("/api/portfolio").json()["holdings"]
    assert h2["units"] == 15.0


def test_drawdown_index_ignores_new_money():
    from finresearch.portfolio.metrics import drawdown

    d = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    # 100 -> 120 -> 108 with no flows: index 1.20 then 1.08, 10 % below the peak
    assert drawdown([(d[0], 100, 100), (d[1], 120, 100), (d[2], 108, 100)])[0] == -10.0
    # doubling the value by investing 100 more is not a gain; selling 50 is not a loss
    assert drawdown([(d[0], 100, 100), (d[1], 200, 200), (d[2], 150, 150)])[0] == 0.0
    assert drawdown([(d[0], 100, 100)])[0] is None


def test_alert_metrics_contract(client):
    from finresearch.alerts.portfolio import portfolio_metrics
    from finresearch.db import session_scope

    with session_scope() as s:
        m, reason = portfolio_metrics(s)
    assert reason is None and m["ltcg_headroom_inr"][0] is None  # no holdings yet: every metric says why
    client.post("/api/portfolio/import/tradebook", headers=ORIGIN,
                json={"filename": "tb.csv", "content_b64": b64(ZERODHA.encode()), "dry_run": False})  # fmt: skip
    assert (
        client.put(
            "/api/portfolio/targets", headers=ORIGIN, json={"Stocks": 50, "Debt funds": 40}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/portfolio/targets", headers=ORIGIN, json={"Stocks": 60, "Equity funds": 40}
        ).status_code
        == 200
    )
    snap = client.get("/api/portfolio").json()  # values the portfolio and records the day's snapshot
    assert snap["complete"] and snap["drift"][0] == {
        "label": "Stocks",
        "weight_pct": 100.0,
        "target_pct": 60.0,
        "drift_pp": 40.0,
    }
    with session_scope() as s:
        m, reason = portfolio_metrics(s)
    assert reason is None
    assert m["allocation_drift_pp"][0] == D("40.0")
    assert m["ltcg_headroom_inr"][0] == D("125000.0")  # no sales in the current financial year
    assert m["drawdown_pct"][0] is None and "two days" in m["drawdown_pct"][1]

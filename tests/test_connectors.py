"""Broker connections: read-only enforcement, golden mappings of recorded/synthetic API responses, merge rules, the
API flow (masked secrets, OAuth callback state), the statement inbox and the monitor schedule.

Every broker response here is synthetic, shaped after the samples in the official docs (see each connector's
docstring); no real credentials, accounts or network. httpx.MockTransport records every request so the tests can
assert that nothing but GETs (and the one login POST) ever leaves the app.
"""

from __future__ import annotations

import ast
import base64
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from finresearch.fincalc.dates import IST
from finresearch.portfolio.connectors import all_classes, base
from finresearch.portfolio.connectors.base import (
    FORBIDDEN_ATTR,
    BrokerHolding,
    BrokerTrade,
    ForbiddenRequest,
    ReadOnlyHttp,
    ReconnectNeeded,
)
from finresearch.portfolio.connectors.totp import hotp, totp

ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
SEED = "JBSWY3DPEHPK3PXP"  # the classic example base32 seed (not anyone's)
GROWW_KEY = "groww-totp-api-key-synthetic-000000000000"
ACCESS = "access-token-synthetic-9999999999"

# ------------------------------------------------------------------ synthetic responses (docs-sample shapes)
GROWW = {
    "/v1/token/api/access": {"token": ACCESS, "tokenRefId": "r1", "sessionName": "s", "expiry": "2026-10-01T06:00:00"},
    "/v1/holdings/user": {"status": "SUCCESS", "payload": {"holdings": [
        {"isin": "INE000A01011", "trading_symbol": "EXMPL", "quantity": 10, "average_price": 100, "pledge_quantity": 0,
         "demat_locked_quantity": 0, "groww_locked_quantity": 0, "repledge_quantity": 0, "t1_quantity": 2,
         "demat_free_quantity": 10, "corporate_action_additional_quantity": 0, "active_demat_transfer_quantity": 0},
        {"isin": "INE000B01012", "trading_symbol": "EXBANK", "quantity": 5, "average_price": 480.5, "t1_quantity": 0},
        {"isin": "INE000C01013", "trading_symbol": "GONE", "quantity": 0, "average_price": 10, "t1_quantity": 0},
    ]}},
    "/v1/positions/user": {"status": "SUCCESS", "payload": {"positions": [
        {"trading_symbol": "EXMPL", "credit_quantity": 2, "credit_price": 250, "debit_quantity": 0, "debit_price": 0,
         "exchange": "NSE", "symbol_isin": "INE000A01011", "quantity": 2, "product": "CNC", "net_price": 250,
         "realised_pnl": 0}]}},
    "/v1/order/list": {"status": "SUCCESS", "payload": {"order_list": [
        {"groww_order_id": "GO1", "trading_symbol": "EXMPL", "order_status": "EXECUTED", "quantity": 2, "price": 0,
         "filled_quantity": 2, "average_fill_price": 250, "exchange": "NSE", "transaction_type": "BUY",
         "created_at": "2026-09-30T10:15:00", "product": "CNC"},
        {"groww_order_id": "GO2", "trading_symbol": "EXBANK", "order_status": "REJECTED", "quantity": 1,
         "filled_quantity": 0, "average_fill_price": 0, "exchange": "NSE", "transaction_type": "SELL",
         "created_at": "2026-09-30T11:00:00"}]}},
    "/v1/margins/detail/user": {"status": "SUCCESS", "payload": {"clear_cash": 1234.5, "net_margin_used": 0,
                                                                  "collateral_available": 0}},
}  # fmt: skip
KITE = {
    "/session/token": {"status": "success", "data": {"user_id": "AB1234", "access_token": ACCESS,
                                                      "public_token": "p", "refresh_token": ""}},
    "/portfolio/holdings": {"status": "success", "data": [
        {"tradingsymbol": "EXMPL", "exchange": "NSE", "instrument_token": 1, "isin": "INE000A01011", "product": "CNC",
         "price": 0, "quantity": 8, "used_quantity": 0, "t1_quantity": 2, "realised_quantity": 8,
         "average_price": 161, "last_price": 352.95, "close_price": 352.35, "pnl": 191.95}]},
    "/portfolio/positions": {"status": "success", "data": {"net": [], "day": []}},
    "/trades": {"status": "success", "data": [
        {"trade_id": "10000000", "order_id": "200000000000000", "exchange": "NSE", "tradingsymbol": "EXMPL",
         "instrument_token": 1, "product": "CNC", "average_price": 420.65, "quantity": 1,
         "exchange_order_id": "300000000000000", "transaction_type": "BUY", "fill_timestamp": "2026-09-30 09:16:39",
         "order_timestamp": "09:16:39", "exchange_timestamp": "2026-09-30 09:16:39"},
        {"trade_id": "10000001", "order_id": "2", "exchange": "NFO", "tradingsymbol": "NIFTY26OCTFUT", "product": "NRML",
         "average_price": 25000, "quantity": 75, "transaction_type": "BUY", "fill_timestamp": "2026-09-30 09:20:00"}]},
    "/mf/holdings": {"status": "success", "data": [
        {"folio": "3108290884", "average_price": 78.43, "last_price": 84.86, "last_price_date": "",
         "pledged_quantity": 0, "fund": "EXAMPLE TAX PLAN - DIRECT PLAN", "tradingsymbol": "INF000K01NT8",
         "pnl": 0, "quantity": 382.488}]},
    "/user/margins": {"status": "success", "data": {"equity": {"net": 5000.5, "available": {"live_balance": 4000},
                                                                "utilised": {"debits": 1000.5}}}},
}  # fmt: skip
UPSTOX = {
    "/v2/login/authorization/token": {"access_token": ACCESS, "user_id": "UX1", "extended_token": "ext"},
    "/v2/portfolio/long-term-holdings": {"status": "success", "data": [
        {"isin": "INE000B01012", "cnc_used_quantity": 0, "company_name": "EXAMPLE BANK LTD.", "haircut": 0.2,
         "product": "D", "quantity": 36, "trading_symbol": "EXBANK", "tradingsymbol": "EXBANK", "last_price": 17.05,
         "close_price": 17.05, "pnl": -61.2, "instrument_token": "NSE_EQ|INE000B01012", "average_price": 18.75,
         "t1_quantity": 0, "exchange": "NSE"}]},
    "/v2/portfolio/short-term-positions": {"status": "success", "data": []},
    "/v2/charges/historical-trades": {"status": "success", "data": [
        {"exchange": "NSE", "segment": "EQ", "option_type": "", "quantity": 36, "amount": 675, "trade_id": "U1",
         "trade_date": "2024-04-10", "transaction_type": "BUY", "scrip_name": "EXAMPLE BANK LTD.", "strike_price": "",
         "expiry": "", "price": 18.75, "isin": "INE000B01012", "symbol": "EXBANK", "instrument_token": "x"},
        {"exchange": "NSE", "segment": "FO", "quantity": 50, "trade_id": "U2", "trade_date": "2024-05-10",
         "transaction_type": "BUY", "price": 10, "symbol": "NIFTY"}]},
}  # fmt: skip
DHAN = {
    "/app/generateAccessToken": {"dhanClientId": "1000000009", "accessToken": ACCESS,
                                 "expiryTime": "2026-10-01T16:00:00"},
    "/v2/holdings": [{"exchange": "ALL", "tradingSymbol": "EXMPL", "securityId": "1330", "isin": "INE000A01011",
                      "totalQty": 12, "dpQty": 12, "t1Qty": 0, "availableQty": 12, "collateralQty": 0,
                      "avgCostPrice": 101.5}],
    "/v2/positions": [{"dhanClientId": "1000000009", "tradingSymbol": "TCS", "securityId": "11536",
                       "positionType": "LONG", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY", "buyAvg": 3345.8,
                       "netQty": 40, "unrealizedProfit": 6122.0}],
    "/v2/fundlimit": {"dhanClientId": "1000000009", "availabelBalance": 98440.0, "withdrawableBalance": 90000,
                      "utilizedAmount": 1560},
}  # fmt: skip
DHAN_TRADES = [
    {"dhanClientId": "1000000009", "orderId": "O1", "exchangeOrderId": "EO1", "exchangeTradeId": "ET1",
     "transactionType": "BUY", "exchangeSegment": "NSE_EQ", "productType": "CNC", "tradingSymbol": "EXMPL",
     "isin": "INE000A01011", "tradedQuantity": 2, "tradedPrice": 110.25, "exchangeTime": "2026-09-29 10:00:00"},
    {"orderId": "O2", "exchangeTradeId": "ET2", "transactionType": "SELL", "exchangeSegment": "NSE_FNO",
     "tradingSymbol": "NIFTY-Oct2026-FUT", "tradedQuantity": 75, "tradedPrice": 1, "exchangeTime": "2026-09-29"},
]  # fmt: skip


class Recorder:
    """An httpx MockTransport serving the synthetic responses and recording every request."""

    def __init__(self, routes: dict[str, object], status: int = 200) -> None:
        self.routes, self.status, self.requests = routes, status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/v2/trades/"):
            page = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=DHAN_TRADES if page == "0" else [])
        if path not in self.routes:
            return httpx.Response(404, json={"message": f"no route {path}"})
        return httpx.Response(self.status, json=self.routes[path])


@pytest.fixture
def transport(monkeypatch):
    def install(routes: dict[str, object], status: int = 200) -> Recorder:
        rec = Recorder(routes, status)
        monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(rec))
        return rec

    return install


def run(coro):
    import asyncio

    return asyncio.run(coro)


# ------------------------------------------------------------------ TOTP golden values (RFC 6238 Appendix B)
@pytest.mark.parametrize(("t", "code8"), [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                                          (1234567890, "89005924"), (2000000000, "69279037"),
                                          (20000000000, "65353130")])  # fmt: skip
def test_totp_rfc6238_vectors(t, code8):
    key = b"12345678901234567890"
    assert hotp(key, t // 30, digits=8) == code8
    seed = base64.b32encode(key).decode()
    assert totp(seed, t, digits=8) == code8
    assert totp(seed, t) == code8[-6:]


def test_totp_rejects_a_six_digit_code_as_seed():
    with pytest.raises(ValueError):
        totp("123456!")


# ------------------------------------------------------------------ read-only enforcement
ORDER_PATHS = ["/order/create", "/order/modify", "/order/cancel", "/orders/regular", "/orders/regular/1",
               "/gtt/triggers", "/order/place", "/v2/order/place", "/orders", "/super/orders", "/forever/orders",
               "/v1/order/create", "/rest/secure/angelbroking/order/v1/placeOrder"]  # fmt: skip


@pytest.mark.parametrize("cls", all_classes(), ids=lambda c: c.key)
def test_connectors_expose_no_order_methods(cls):
    names = [n for n in dir(cls) if not n.startswith("__")]
    bad = [n for n in names if FORBIDDEN_ATTR.search(n)]
    assert not bad, f"{cls.__name__} exposes order-shaped attributes: {bad}"
    mod = __import__(cls.__module__, fromlist=["READ_PATHS", "AUTH_PATHS"])
    import re

    for p in ORDER_PATHS:
        assert not any(re.fullmatch(rp, p) for rp in mod.READ_PATHS), (cls.key, p)
        assert not any(re.fullmatch(ap, p) for ap in mod.AUTH_PATHS), (cls.key, p)


def test_no_vendor_sdk_is_imported():
    root = Path(__file__).resolve().parents[1] / "src" / "finresearch" / "portfolio" / "connectors"
    banned = {"growwapi", "kiteconnect", "upstox_client", "dhanhq", "SmartApi", "fyers_apiv3"}
    for f in root.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned, f
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned, f


def test_http_layer_refuses_anything_but_allowlisted_reads(transport):
    rec = transport({"/holdings": {"ok": 1}})
    http = ReadOnlyHttp("https://broker.example", read_paths=(r"/holdings",), auth_paths=(r"/login",))
    assert run(http.get("/holdings")) == {"ok": 1}
    for coro in (http.get("/orders"), http.auth_post("/orders", json={}), http._send("PUT", "/holdings"),
                 http._send("DELETE", "/holdings"), http._send("POST", "/holdings")):  # fmt: skip
        with pytest.raises(ForbiddenRequest):
            run(coro)
    assert len(rec.requests) == 1  # the refused requests never reached the transport


def test_session_errors_are_redacted(transport):
    transport({"/v1/holdings/user": {"message": f"token {ACCESS} expired"}}, status=401)
    from finresearch.portfolio.connectors.groww import GrowwConnector

    c = GrowwConnector({"api_key": GROWW_KEY, "totp_secret": SEED}, ACCESS)
    with pytest.raises(ReconnectNeeded) as e:
        run(c.holdings())
    assert ACCESS not in str(e.value) and "[redacted]" in str(e.value)


# ------------------------------------------------------------------ golden mappings per broker
def test_groww_login_and_reads(transport):
    from finresearch.portfolio.connectors.groww import GrowwConnector

    rec = transport(GROWW)
    c = GrowwConnector({"api_key": GROWW_KEY, "totp_secret": SEED})
    grant = run(c.login())
    assert grant.token == ACCESS and grant.expires_at.astimezone(IST).hour == 6
    login = rec.requests[0]
    assert login.method == "POST" and login.url.path == "/v1/token/api/access"
    assert login.headers["authorization"] == f"Bearer {GROWW_KEY}"
    assert json.loads(login.content)["key_type"] == "totp" and len(json.loads(login.content)["totp"]) == 6
    c.token = grant.token
    hs = run(c.holdings())
    assert [(h.isin, h.symbol, h.quantity, h.avg_price) for h in hs] == [
        ("INE000A01011", "EXMPL", D(12), D(100)), ("INE000B01012", "EXBANK", D(5), D("480.5"))]  # fmt: skip
    pos = run(c.positions())
    assert pos[0].symbol == "EXMPL" and pos[0].quantity == 2 and pos[0].product == "CNC"
    tr = run(c.trades(date(2026, 9, 1), date(2026, 9, 30)))
    assert [(t.day, t.side, t.quantity, t.price, t.order_id) for t in tr] == [
        (date(2026, 9, 30), "buy", D(2), D(250), "GO1")
    ]  # the rejected order is not a trade
    assert run(c.funds())["cash"] == D("1234.5")
    reads = rec.requests[1:]
    assert all(r.method == "GET" for r in reads)
    assert all(r.headers["authorization"] == f"Bearer {ACCESS}" and r.headers["x-api-version"] == "1.0"
               for r in reads)  # fmt: skip


def test_zerodha_oauth_and_reads(transport):
    from urllib.parse import parse_qs, urlparse

    from finresearch.portfolio.connectors.zerodha import ZerodhaConnector, checksum

    rec = transport(KITE)
    c = ZerodhaConnector({"api_key": "kitekey", "api_secret": "kitesecret"})
    url = c.login_url("http://127.0.0.1:8710/api/connections/zerodha/callback", "STATE123")
    q = parse_qs(urlparse(url).query)
    assert q["api_key"] == ["kitekey"] and q["v"] == ["3"]
    assert parse_qs(q["redirect_params"][0]) == {"finresearch_state": ["STATE123"]}
    grant = run(c.exchange_code("REQTOKEN", "unused"))
    assert grant.token == ACCESS and grant.extra["connected_as"] == "AB1234"
    body = parse_qs(rec.requests[0].content.decode())
    assert body["checksum"] == [checksum("kitekey", "REQTOKEN", "kitesecret")]
    c.token = grant.token
    hs = run(c.holdings())
    assert (hs[0].isin, hs[0].symbol, hs[0].quantity, hs[0].avg_price) == (
        "INE000A01011",
        "EXMPL",
        D(10),
        D(161),
    )
    tr = run(c.trades(date(2026, 9, 30), date(2026, 9, 30)))
    assert [(t.symbol, t.quantity, t.price, t.order_id) for t in tr] == [
        ("EXMPL", D(1), D("420.65"), "300000000000000")
    ]  # the NFO fill is not a portfolio trade
    mf = run(c.mf_holdings())
    assert (mf[0].asset_type, mf[0].isin, mf[0].quantity) == ("mf", "INF000K01NT8", D("382.488"))
    assert run(c.funds()) == {"net": D("5000.5"), "cash": D(4000), "margin_used": D("1000.5")}
    assert all(r.method == "GET" and r.headers["authorization"] == f"token kitekey:{ACCESS}"
               for r in rec.requests[1:])  # fmt: skip


def test_checksum_is_sha256_of_key_token_secret():
    import hashlib

    from finresearch.portfolio.connectors.zerodha import checksum

    assert checksum("a", "b", "c") == hashlib.sha256(b"abc").hexdigest()
    # a hand-checked value: sha256("abc") from FIPS 180-2 Appendix B.1
    assert checksum("a", "b", "c") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_upstox_oauth_and_history(transport):
    from urllib.parse import parse_qs, urlparse

    from finresearch.portfolio.connectors.upstox import UpstoxConnector

    rec = transport(UPSTOX)
    c = UpstoxConnector({"api_key": "cid", "api_secret": "csecret"})
    q = parse_qs(urlparse(c.login_url("http://127.0.0.1:8710/cb", "S1")).query)
    assert q == {"client_id": ["cid"], "redirect_uri": ["http://127.0.0.1:8710/cb"], "response_type": ["code"],
                 "state": ["S1"]}  # fmt: skip
    grant = run(c.exchange_code("CODE", "http://127.0.0.1:8710/cb"))
    assert grant.token == ACCESS and grant.expires_at.astimezone(IST).strftime("%H:%M") == "03:30"
    assert parse_qs(rec.requests[0].content.decode())["grant_type"] == ["authorization_code"]
    c.token = grant.token
    hs = run(c.holdings())
    assert (hs[0].name, hs[0].isin, hs[0].quantity, hs[0].avg_price) == ("EXAMPLE BANK LTD.", "INE000B01012", D(36),
                                                                          D("18.75"))  # fmt: skip
    tr = run(c.trades(date(2024, 1, 1), date(2026, 9, 30)))
    assert [(t.day, t.isin, t.quantity, t.price, t.trade_id) for t in tr] == [
        (date(2024, 4, 10), "INE000B01012", D(36), D("18.75"), "U1")
    ]
    hist = next(r for r in rec.requests if r.url.path == "/v2/charges/historical-trades")
    assert hist.url.params["segment"] == "EQ" and hist.url.params["start_date"] == "2024-01-01"


def test_dhan_totp_login_and_reads(transport):
    from finresearch.portfolio.connectors.dhan import DhanConnector

    rec = transport(DHAN)
    c = DhanConnector({"client_id": "1000000009", "pin": "1234", "totp_secret": SEED})
    grant = run(c.login())
    assert grant.token == ACCESS and grant.expires_at == datetime(2026, 10, 1, 16, 0, tzinfo=IST)
    login = rec.requests[0]
    assert login.method == "POST" and login.url.host == "auth.dhan.co"
    c.token = grant.token
    hs = run(c.holdings())
    assert (hs[0].isin, hs[0].quantity, hs[0].avg_price) == ("INE000A01011", D(12), D("101.5"))
    tr = run(c.trades(date(2026, 9, 1), date(2026, 9, 30)))
    assert [(t.side, t.quantity, t.price, t.trade_id, t.order_id) for t in tr] == [
        ("buy", D(2), D("110.25"), "ET1", "EO1")
    ]
    assert run(c.funds())["cash"] == D(98440)
    assert run(c.positions())[0].product == "INTRADAY"
    assert all(r.method == "GET" and r.headers["access-token"] == ACCESS for r in rec.requests[1:])


def test_dhan_pasted_token_needs_no_login_call(transport):
    from finresearch.portfolio.connectors.dhan import DhanConnector

    rec = transport(DHAN)
    grant = run(DhanConnector({"client_id": "1", "access_token": "pasted-token-xyz"}).login())
    assert grant.token == "pasted-token-xyz" and not rec.requests


# ------------------------------------------------------------------ lots: the broker-average baseline
def test_broker_average_opening_keeps_cost_but_not_date():
    from finresearch.portfolio.lots import Event, build_lots

    book = build_lots([Event(1, date(2026, 9, 29), "opening", D(10), D(100), D(1000),
                             meta={"cost_basis": "broker_average", "statement_opening": True}),
                       Event(2, date(2026, 9, 30), "sell", D(4), D(150), D(600))])  # fmt: skip
    lot = book.lots[0]
    assert lot.acquired is None and lot.cost_per_unit == D(100) and lot.open_quantity == D(6)
    assert book.disposals[0].cost == D(400) and book.disposals[0].gain == D(200)
    plain = build_lots([Event(1, date(2026, 9, 29), "opening", D(10), D(100), D(1000))])
    assert plain.lots[0].cost_per_unit is None  # a CAS opening balance without a date stays unknown


def test_holdings_statement_parsers():
    from openpyxl import Workbook

    from finresearch.portfolio.importers import parse_holdings_statement

    wb = Workbook()
    ws = wb.active
    for r in (["Client ID", "XX0000"], ["Holdings as on 2026-09-30"], [],
              ["Symbol", "ISIN", "Sector", "Quantity Available", "Quantity Discrepant", "Quantity Long Term",
               "Quantity Pledged (Margin)", "Quantity Pledged (Loan)", "Average Price", "Previous Closing Price",
               "Unrealized P&L"],
              ["EXMPL", "INE000A01011", "Textiles", 8, 0, 8, 2, 0, 161.5, 352.35, 1000],
              ["Total", "", "", "", "", "", "", "", "", "", 1000]):  # fmt: skip
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    broker, hs = parse_holdings_statement(buf.getvalue(), "holdings.xlsx")
    assert broker == "zerodha"
    assert [(h.symbol, h.isin, h.quantity, h.avg_price, h.last_price) for h in hs] == [
        ("EXMPL", "INE000A01011", D(10), D("161.5"), D("352.35"))
    ]  # available 8 + pledged 2
    groww = ("Stock Name,ISIN,Quantity,Average buy price,Buy value,Closing price,Closing value,Unrealised P&L\n"
             "Example Bank Ltd,INE000B01012,5,480.5,2402.5,500,2500,97.5\n")  # fmt: skip
    broker, hs = parse_holdings_statement(groww.encode(), "Stocks_Holdings.csv")
    assert broker == "groww" and (hs[0].name, hs[0].quantity, hs[0].avg_price) == ("Example Bank Ltd", D(5),
                                                                                   D("480.5"))  # fmt: skip


# ------------------------------------------------------------------ database: merge rules
@pytest.fixture
def db(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                       "portfolio_snapshot, portfolio_setting, broker_connection, broker_sync_log, alert CASCADE"))  # fmt: skip
    yield session_scope
    # leave no connections behind: other tests run the monitor tick, which would try to sync them
    with session_scope() as s:
        s.execute(text("TRUNCATE broker_connection, broker_sync_log, portfolio_disposal, portfolio_lot, portfolio_txn, "
                       "portfolio_holding, portfolio_import, alert CASCADE"))  # fmt: skip


NOW = datetime(2026, 9, 30, 11, 0, tzinfo=UTC)  # 16:30 IST
TODAY = date(2026, 9, 30)


def _h(isin, sym, q, avg):
    return BrokerHolding(name=sym, quantity=D(q), isin=isin, symbol=sym, avg_price=D(avg))


def _t(day, side, sym, isin, q, price, tid, oid=None):
    return BrokerTrade(day=day, side=side, quantity=D(q), price=D(price), name=sym, isin=isin, symbol=sym,
                       exchange="NSE", trade_id=tid, order_id=oid)  # fmt: skip


def _units(s, account):
    from finresearch.portfolio.service import lot_units

    return {ik: u for (ik, acc), u in lot_units(s).items() if acc == account}


def test_baseline_trades_and_idempotent_resync(db):
    from finresearch.portfolio.connectors.merge import merge_sync

    holdings = [_h("INE000A01011", "EXMPL", 10, 100), _h("INE000B01012", "EXBANK", 5, "480.5")]
    trades = [_t(date(2026, 9, 28), "buy", "EXMPL", "INE000A01011", 3, 95, "T0"),  # history within the holdings
              _t(TODAY, "buy", "EXMPL", "INE000A01011", 2, 110, "T1")]  # fmt: skip
    with db() as s:
        res = merge_sync(s, account="Groww", source="groww_api", label="Groww", holdings=holdings, trades=trades,
                         today=TODAY, now=NOW, holdings_include_today=False)  # fmt: skip
        # EXMPL: the 28-Sep trade is history, so the baseline covers only the 7 older units at
        # (10 x 100 - 3 x 95) / 7 = 102.1429, dated the day before; today's buy is not in the holdings yet
        assert res.baselines[0] == {
            "name": "EXMPL",
            "units": "7",
            "day": "2026-09-27",
            "avg_price": "102.1429",
        }
        assert res.baselines[1]["day"] == "2026-09-29" and res.added == 2 and res.covered_by_baseline == 0
        assert _units(s, "Groww") == {"ISIN:INE000A01011": D(12), "ISIN:INE000B01012": D(5)}
        from sqlalchemy import select

        from finresearch.db.models import PortfolioLot

        cpu = sorted(lot.cost_per_unit for lot in s.scalars(select(PortfolioLot)))
        assert cpu == [
            D(95),
            D("102.1429"),
            D(110),
            D("480.5"),
        ]  # P&L works on the baseline; its date is unknown
        rec = {r["ikey"]: r for r in res.reconciliation}
        # Groww's holdings exclude today's buy (holdings_include_today False), so they stand for the end of 29-Sep:
        # the app's units on that day are 7 (baseline) + 3 (28-Sep) = 10 = the broker's 10, not 12 - 10 = 2 off.
        # Before #266 this compared against today's lots and reported a difference on every trading day.
        assert rec["ISIN:INE000A01011"]["diff"] == "0.000" and rec["ISIN:INE000A01011"]["ok"]
        assert res.as_dict()["reconciled"]
    # the next day the broker's holdings include it; the same trades come back in the overlap window
    holdings2 = [_h("INE000A01011", "EXMPL", 12, "101.67"), _h("INE000B01012", "EXBANK", 5, "480.5")]
    with db() as s:
        res = merge_sync(s, account="Groww", source="groww_api", label="Groww", holdings=holdings2, trades=trades,
                         today=date(2026, 10, 1), now=NOW, holdings_include_today=False)  # fmt: skip
        assert res.added == 0 and res.duplicates == 2 and not res.baselines
        assert all(r["ok"] for r in res.reconciliation)


def test_cross_source_duplicates_and_conflicts_are_not_added(db):
    from finresearch.portfolio.connectors.merge import merge_sync
    from finresearch.portfolio.importers import parse_tradebook
    from finresearch.portfolio.service import apply

    csv = ("Stock name,Symbol,ISIN,Type,Quantity,Value,Exchange,Exchange Order Id,Execution date and time,"
           "Order status\nExample Ltd,EXMPL,INE000A01011,BUY,10,1000,NSE,X1,01-09-2026 10:30 AM,Executed\n"
           "Example Ltd,EXMPL,INE000A01011,BUY,4,480,NSE,X2,02-09-2026 10:30 AM,Executed\n")  # fmt: skip
    with db() as s:
        apply(
            s,
            parse_tradebook(csv.encode(), "groww.csv"),
            filename="groww.csv",
            sha256="a" * 64,
            saved_path=None,
        )
        trades = [
            _t(date(2026, 9, 1), "buy", "EXMPL", "INE000A01011", 6, 100, "F1"),
            _t(date(2026, 9, 1), "buy", "EXMPL", "INE000A01011", 4, 100, "F2"),  # two fills of order X1
            _t(date(2026, 9, 2), "buy", "EXMPL", "INE000A01011", 5, 120, "F3"),  # more than the CSV's 4
            _t(date(2026, 9, 3), "buy", "EXMPL", "INE000A01011", 1, 125, "F4"),
        ]  # new
        res = merge_sync(s, account="Groww", source="groww_api", label="Groww",
                         holdings=[_h("INE000A01011", "EXMPL", 15, 110)], trades=trades, today=TODAY, now=NOW)  # fmt: skip
        assert not res.baselines  # the account already has history for it
        assert [c["matched"] for c in res.cross_source] == ["same day, side and total units"] * 2
        assert len(res.conflicts) == 1 and res.conflicts[0]["day"] == "2026-09-02"
        assert res.added == 1
        assert _units(s, "Groww") == {"ISIN:INE000A01011": D(15)}
        assert all(r["ok"] for r in res.reconciliation)


def test_order_id_match_and_manual_holding_is_never_overwritten(db):
    from finresearch.portfolio.connectors.merge import merge_sync
    from finresearch.portfolio.service import manual_txn

    with db() as s:
        manual_txn(s, {"asset_type": "stock", "name": "Example Bank", "account": "Manual", "isin": "INE000B01012",
                       "day": date(2025, 1, 1), "kind": "buy", "quantity": D(5), "price": D(400)})  # fmt: skip
        res = merge_sync(s, account="Zerodha", source="zerodha_api", label="Zerodha",
                         holdings=[_h("INE000B01012", "EXBANK", 5, 480)], trades=[], today=TODAY, now=NOW)  # fmt: skip
        assert not res.baselines and res.baseline_skipped[0]["held_in"] == ["Manual"]
        assert res.reconciliation[0]["status"] == "missing_in_app"
        assert _units(s, "Manual") == {"ISIN:INE000B01012": D(5)}  # untouched


def test_mf_from_broker_reconciles_by_instrument(db):
    from finresearch.portfolio.connectors.merge import merge_sync
    from finresearch.portfolio.service import manual_txn

    mf = BrokerHolding(name="EXAMPLE TAX PLAN", quantity=D("382.488"), isin="INF000K01NT8", asset_type="mf",
                       avg_price=D("78.43"))  # fmt: skip
    with db() as s:
        manual_txn(s, {"asset_type": "mf", "name": "Example Tax Plan", "account": "CAMS · folio 1",
                       "isin": "INF000K01NT8", "day": date(2024, 1, 1), "kind": "buy", "quantity": D("382.488"),
                       "price": D(70)})  # fmt: skip
        res = merge_sync(s, account="Zerodha", source="zerodha_api", label="Zerodha", holdings=[], trades=[],
                         today=TODAY, now=NOW, mf_holdings=[mf])  # fmt: skip
        assert not res.baselines
        mfrow = next(r for r in res.reconciliation if r["ikey"] == "ISIN:INF000K01NT8")
        assert mfrow["ok"] and mfrow["account"] == "Zerodha MF"


# ------------------------------------------------------------------ API flow
@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app

    app = create_app(clock=lambda: NOW)
    with TestClient(app) as c:
        yield c


def test_api_secrets_masked_sync_and_disconnect(client, transport):
    rec = transport(GROWW)
    r = client.put("/api/connections/groww", headers=ORIGIN,
                   json={"config": {"api_key": GROWW_KEY, "totp_secret": SEED}})  # fmt: skip
    assert r.status_code == 200, r.text
    assert GROWW_KEY not in r.text and SEED not in r.text
    assert r.json()["config"]["api_key_set"] and r.json()["status"] == "ready"
    bad = client.put("/api/connections/groww", headers=ORIGIN, json={"config": {"totp_secret": "123456!"}})
    assert bad.status_code == 422 and "123456!" not in bad.text
    # sending the masked value back keeps the secret
    masked = r.json()["config"]["api_key"]
    client.put(
        "/api/connections/groww", headers=ORIGIN, json={"config": {"api_key": masked}, "auto_sync": False}
    )
    listing = client.get("/api/connections").text
    assert GROWW_KEY not in listing and SEED not in listing and ACCESS not in listing
    out = client.post("/api/connections/groww/sync", headers=ORIGIN).json()
    assert out["status"] == "ok", out
    assert out["summary"]["added"] == 1 and len(out["summary"]["baselines"]) == 2
    assert {r.method for r in rec.requests} == {"GET", "POST"}
    assert [r.url.path for r in rec.requests if r.method == "POST"] == ["/v1/token/api/access"]
    conn = next(c for c in client.get("/api/connections").json()["connections"] if c["key"] == "groww")
    assert conn["status"] == "connected" and conn["last_sync_at"] and conn["funds"]["cash"] == 1234.5
    assert conn["positions"][0]["symbol"] == "EXMPL" and not conn["auto_sync"]
    log = client.get("/api/connections/log?key=groww").json()
    assert log[0]["status"] == "ok" and ACCESS not in json.dumps(log)
    imports = client.get("/api/portfolio/imports").json()
    assert {i["kind"] for i in imports} == {"api", "baseline"}
    assert client.delete("/api/connections/groww", headers=ORIGIN).json() == {"deleted": True}
    assert next(c for c in client.get("/api/connections").json()["connections"]
                if c["key"] == "groww")["status"] == "not_connected"  # fmt: skip


def test_expired_session_marks_reconnect_and_alerts_once(client, transport):
    transport({**GROWW, "/v1/holdings/user": {"message": "expired"}}, status=200)
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors.sync import sync_now

    client.put(
        "/api/connections/zerodha", headers=ORIGIN, json={"config": {"api_key": "k", "api_secret": "s"}}
    )
    res = run(sync_now("zerodha", trigger="scheduled", now=NOW))
    assert res["status"] == "reconnect"
    run(sync_now("zerodha", trigger="scheduled", now=NOW))
    with session_scope() as s:
        from sqlalchemy import func, select

        from finresearch.db.models import Alert

        assert s.get(BrokerConnection, "zerodha").status == "reconnect"
        assert s.scalar(select(func.count()).select_from(Alert).where(Alert.kind == "broker_reconnect")) == 1


def test_oauth_callback_checks_state(client, transport):
    transport(KITE)
    client.put(
        "/api/connections/zerodha", headers=ORIGIN, json={"config": {"api_key": "kk", "api_secret": "ss"}}
    )
    wrong = client.get("/api/connections/zerodha/callback?request_token=RT&finresearch_state=nope",
                       follow_redirects=False)  # fmt: skip
    assert wrong.status_code == 303 and "connect_error" in wrong.headers["location"]
    url = client.get("/api/connections/zerodha/login-url").json()["url"]
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(parse_qs(urlparse(url).query)["redirect_params"][0])["finresearch_state"][0]
    ok = client.get(f"/api/connections/zerodha/callback?request_token=RT&status=success&finresearch_state={state}",
                    follow_redirects=False)  # fmt: skip
    assert ok.status_code == 303 and ok.headers["location"].endswith("/profile?connected=zerodha#connections")
    conn = next(c for c in client.get("/api/connections").json()["connections"] if c["key"] == "zerodha")
    assert conn["status"] == "connected" and conn["connected_as"] == "AB1234"
    replay = client.get(f"/api/connections/zerodha/callback?request_token=RT&finresearch_state={state}",
                        follow_redirects=False)  # fmt: skip
    assert "connect_error" in replay.headers["location"]  # the state is single-use
    out = client.post("/api/connections/zerodha/sync", headers=ORIGIN).json()
    assert out["status"] == "ok" and out["summary"]["read"]["mf"] == 1


def test_portfolio_rows_carry_their_sources(client, transport):
    transport(GROWW)
    client.put(
        "/api/connections/groww", headers=ORIGIN, json={"config": {"api_key": GROWW_KEY, "totp_secret": SEED}}
    )
    client.post("/api/connections/groww/sync", headers=ORIGIN)
    from finresearch.db import session_scope
    from finresearch.portfolio.report import snapshot

    with session_scope() as s:
        rows = snapshot(s, {}, TODAY)["holdings"]
    ex = next(r for r in rows if r["isin"] == "INE000A01011")
    assert ex["sources"] == ["groww_api"] and ex["broker_baseline"] and ex["cost_known"]
    summary = client.get("/api/connections/summary").json()
    assert summary[0]["key"] == "groww" and summary[0]["last_sync_at"]


def test_holdings_statement_through_the_import_route(client):
    groww = ("Stock Name,ISIN,Quantity,Average buy price,Buy value,Closing price,Closing value,Unrealised P&L\n"
             "Example Bank Ltd,INE000B01012,5,480.5,2402.5,500,2500,97.5\n")  # fmt: skip
    b64 = base64.b64encode(groww.encode()).decode()
    dry = client.post("/api/portfolio/import/tradebook", headers=ORIGIN,
                      json={"filename": "h.csv", "content_b64": b64}).json()  # fmt: skip
    assert dry["dry_run"] and dry["kind"] == "holdings" and len(dry["baselines"]) == 1
    assert client.get("/api/portfolio/imports").json() == []  # nothing written by the dry run
    real = client.post("/api/portfolio/import/tradebook", headers=ORIGIN,
                       json={"filename": "h.csv", "content_b64": b64, "dry_run": False}).json()  # fmt: skip
    assert not real["dry_run"] and real["import_ids"]
    again = client.post("/api/portfolio/import/tradebook", headers=ORIGIN,
                        json={"filename": "h.csv", "content_b64": b64, "dry_run": False})  # fmt: skip
    assert again.status_code == 409


# ------------------------------------------------------------------ statement inbox
def test_inbox_imports_tradebooks_and_holdings_and_waits_for_pdf_password(db, client):
    import os

    from finresearch.portfolio.connectors.inbox import ensure_dirs, scan

    root = ensure_dirs()
    tb = ("symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,"
          "order_execution_time\nEXMPL,INE000A01011,2024-01-10,NSE,EQ,EQ,buy,false,10,100,1001,5001,"
          "2024-01-10T10:00:00\n")  # fmt: skip
    (root / "tradebook.csv").write_text(tb)
    (root / "holdings.csv").write_text("Stock Name,ISIN,Quantity,Average buy price\nExample Bank Ltd,INE000B01012,"
                                       "5,480.5\n")  # fmt: skip
    (root / "cas.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (root / "notes.txt").write_text("ignored")
    old = NOW.timestamp() - 60
    for p in root.iterdir():
        if p.is_file():
            os.utime(p, (old, old))
    client.put("/api/connections/cas_inbox", headers=ORIGIN, json={"enabled": True})
    out = scan(now=NOW)
    by = {f["file"]: f for f in out["files"]}
    assert by["tradebook.csv"]["status"] == "imported" and by["holdings.csv"]["status"] == "imported"
    assert by["cas.pdf"]["status"] == "waiting" and "notes.txt" not in by
    assert sorted(p.name for p in (root / "processed").iterdir()) == ["holdings.csv", "tradebook.csv"]
    assert (root / "cas.pdf").exists()
    # the same tradebook dropped again is recognised and filed away
    (root / "tradebook.csv").write_text(tb)
    os.utime(root / "tradebook.csv", (old, old))
    again = {f["file"]: f for f in scan(now=NOW)["files"]}
    assert again["tradebook.csv"]["status"] == "duplicate"
    # a wrong saved password: the PDF moves on only when the password changes
    client.put("/api/connections/cas_inbox", headers=ORIGIN, json={"config": {"password": "WRONGPASS1"}})
    got = {f["file"]: f for f in scan(now=NOW)["files"]}
    assert got["cas.pdf"]["status"] in ("waiting", "failed")
    listing = client.get("/api/connections").text
    assert "WRONGPASS1" not in listing


# ------------------------------------------------------------------ schedule
def test_scheduled_sync_is_due_once_after_the_close():
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors.sync import due

    row = BrokerConnection(key="groww", enabled=True, auto_sync=True, status="connected", last_sync_day=None)
    at = lambda h, m, d=30: datetime(2026, 9, d, h, m, tzinfo=IST)  # noqa: E731 (Wed 30-Sep-2026)
    assert not due(row, at(15, 0))
    assert due(row, at(16, 5))
    row.last_sync_day = date(2026, 9, 30)
    assert not due(row, at(18, 0))
    row.last_sync_at = datetime(2026, 9, 30, 15, 50, tzinfo=IST)  # a manual "Sync now" before the close
    assert due(row, at(16, 5))  # the after-close read is still owed
    row.last_sync_at = datetime(2026, 9, 30, 16, 6, tzinfo=IST)
    assert not due(row, at(18, 0))
    row.last_sync_at = None
    row.last_sync_day = date(2026, 9, 25)  # the monitor was off for days: catch up at once
    assert due(row, at(10, 0))
    row.last_sync_day = date(2026, 9, 29)
    assert not due(row, at(10, 0))
    row.last_sync_day = date(2026, 10, 2)
    assert not due(row, datetime(2026, 10, 3, 17, 0, tzinfo=IST))  # a Saturday
    row.status, row.token = "reconnect", None
    zerodha = BrokerConnection(
        key="zerodha", enabled=True, auto_sync=True, status="reconnect", last_sync_day=None
    )
    assert not due(zerodha, at(17, 0))  # waiting for the user's login
    row.auto_sync = False
    assert not due(row, at(17, 0))


def test_broker_trade_history_replaces_the_baseline(db):
    from finresearch.portfolio.connectors.merge import merge_sync

    holdings = [_h("INE000B01012", "EXBANK", 36, "18.75"), _h("INE000A01011", "EXMPL", 10, 100)]
    trades = [_t(date(2024, 4, 10), "buy", "EXBANK", "INE000B01012", 30, 18, "U1"),  # 6 units are older
              _t(date(2025, 1, 5), "buy", "EXMPL", "INE000A01011", 4, 90, "U2"),
              _t(date(2025, 2, 5), "buy", "EXMPL", "INE000A01011", 6, "106.6666", "U3")]  # explains all 10  # fmt: skip
    with db() as s:
        res = merge_sync(s, account="Upstox", source="upstox_api", label="Upstox", holdings=holdings, trades=trades,
                         today=TODAY, now=NOW, holdings_include_today=False)  # fmt: skip
        assert res.added == 3 and res.covered_by_baseline == 0
        assert res.baselines == [
            {"name": "EXBANK", "units": "6", "day": "2024-04-09", "avg_price": "22.5000"}
        ]
        # golden: (36 x 18.75 - 30 x 18) / 6 = (675 - 540) / 6 = 22.5
        assert {h["name"]: h["baseline_units"] for h in res.history_used} == {"EXBANK": "6", "EXMPL": "0"}
        assert all(r["ok"] for r in res.reconciliation)
        from sqlalchemy import select

        from finresearch.db.models import PortfolioLot

        lots = sorted((lot.acquired, lot.open_quantity, lot.cost_per_unit) for lot in s.scalars(select(PortfolioLot))
                      if lot.acquired)  # fmt: skip
        assert lots[0] == (date(2024, 4, 10), D(30), D(18))  # real purchase dates for the tax view


def test_failed_scheduled_sync_is_not_retried_the_same_day(client, transport):
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection, BrokerSyncLog
    from finresearch.portfolio.connectors.sync import due, sync_now

    rec = transport({"/v1/token/api/access": {"message": "bad totp"}}, status=401)
    client.put(
        "/api/connections/groww", headers=ORIGIN, json={"config": {"api_key": GROWW_KEY, "totp_secret": SEED}}
    )
    later = datetime(2026, 9, 30, 11, 30, tzinfo=UTC)  # 17:00 IST
    assert run(sync_now("groww", trigger="scheduled", now=NOW))["status"] == "reconnect"
    with session_scope() as s:
        row = s.get(BrokerConnection, "groww")
        assert not due(row, later)  # one scheduled attempt a day: no login storm
        assert due(row, datetime(2026, 10, 1, 11, 0, tzinfo=UTC))  # tomorrow after the close
        from sqlalchemy import func, select

        assert s.scalar(select(func.count()).select_from(BrokerSyncLog)) == 1
    # the code, then one retry with the next 30-second step's code (#266); no more
    assert len(rec.requests) == 2
    conn = next(c for c in client.get("/api/connections").json()["connections"] if c["key"] == "groww")
    assert conn["status"] == "error" and "refused" in conn["next_step"]  # the failure is visible, not "ready"
    assert GROWW_KEY not in json.dumps(conn)


def test_empty_secret_keeps_the_saved_one(client):
    client.put(
        "/api/connections/dhan",
        headers=ORIGIN,
        json={"config": {"client_id": "1", "access_token": "tok-123456"}},
    )
    client.put("/api/connections/dhan", headers=ORIGIN, json={"config": {"access_token": ""}})
    conn = next(c for c in client.get("/api/connections").json()["connections"] if c["key"] == "dhan")
    assert conn["config"]["access_token_set"]
    client.put("/api/connections/dhan", headers=ORIGIN, json={"config": {"clear_access_token": True}})
    conn = next(c for c in client.get("/api/connections").json()["connections"] if c["key"] == "dhan")
    assert not conn["config"]["access_token_set"]


def test_holdings_statement_after_a_short_tradebook_adds_the_older_units(db):
    """Groww order history from Sep-2025 only: 3 bought and 2 sold in the window, Groww holds 40 at ₹813.37. The 39
    units bought earlier become one baseline dated before the history, priced so the open lots cost 40 × 813.37."""
    from finresearch.portfolio.connectors.merge import merge_sync, remember_statement_prices
    from finresearch.portfolio.importers import parse_tradebook
    from finresearch.portfolio.service import apply

    csv = ("Stock name,Symbol,ISIN,Type,Quantity,Value,Exchange,Exchange Order Id,Execution date and time,"
           "Order status\nExample Bank,EXBANK,INE000B01012,BUY,3,3000,NSE,X1,01-09-2025 10:30 AM,Executed\n"
           "Example Bank,EXBANK,INE000B01012,SELL,2,2400,NSE,X2,02-10-2025 10:30 AM,Executed\n")  # fmt: skip
    with db() as s:
        apply(
            s,
            parse_tradebook(csv.encode(), "groww.csv"),
            filename="groww.csv",
            sha256="b" * 64,
            saved_path=None,
        )
        h = BrokerHolding(name="Example Bank", quantity=D(40), isin="INE000B01012", avg_price=D("813.37"),
                          last_price=D("959.5"))  # fmt: skip
        res = merge_sync(s, account="Groww", source="groww_holdings", label="Groww", holdings=[h], trades=[],
                         today=TODAY, now=NOW)  # fmt: skip
        remember_statement_prices(s, account="Groww", holdings=[h], day=TODAY, label="Groww")
        (b,) = res.baselines
        assert b["units"] == "39.000000" and b["day"] == "2025-08-31"
        assert _units(s, "Groww") == {"ISIN:INE000B01012": D(40)}
        assert all(r["ok"] for r in res.reconciliation)
        from sqlalchemy import select

        from finresearch.db.models import PortfolioHolding, PortfolioLot

        hold = s.scalar(select(PortfolioHolding).where(PortfolioHolding.isin == "INE000B01012"))
        lots = s.scalars(select(PortfolioLot).where(PortfolioLot.holding_id == hold.id)).all()
        cost = sum(x.open_quantity * x.cost_per_unit for x in lots if x.open_quantity)
        assert abs(cost - D(40) * D("813.37")) < D("0.01")  # the 2 sold came out of the older units (FIFO)
        assert hold.meta["statement_price"] == {"price": "959.5", "day": "2026-09-30", "source": "Groww"}
        again = merge_sync(s, account="Groww", source="groww_holdings", label="Groww", holdings=[h], trades=[],
                           today=TODAY, now=NOW)  # fmt: skip
        assert not again.baselines  # an opening balance now stands for them


def test_numeric_tradebook_symbol_is_a_bse_code():
    from openpyxl import Workbook

    from finresearch.portfolio.importers import parse_tradebook

    wb = Workbook()
    ws = wb.active
    ws.append(["Stock name", "Symbol", "ISIN", "Type", "Quantity", "Value", "Exchange", "Exchange Order Id",
               "Execution date and time", "Order status"])  # fmt: skip
    ws.append(["Example NCD", 941149, "INE000C07011", "BUY", 30, 30000, "BSE", "X9", "05-01-2026 10:30 AM",
               "Executed"])  # fmt: skip
    buf = io.BytesIO()
    wb.save(buf)
    (t,) = parse_tradebook(buf.getvalue(), "orders.xlsx").txns
    assert t.bse_code == "941149" and t.nse_symbol is None


# ------------------------------------------------------------------ audit regressions (Oct-2026)
def _zerodha(rows: list[tuple[str, str, int, str, str]]) -> bytes:
    head = (
        "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id\n"
    )
    return (head + "".join(f"EXSPLIT,INE000S01011,{d},NSE,EQ,EQ,{side},false,{q},{p},{tid},{tid}\n"
                           for d, side, q, p, tid in rows)).encode()  # fmt: skip


def test_older_units_before_a_tradebook_with_a_split_inside_it(db):
    """Tradebook: 10 bought @ ₹1,000 on 10-Jan-2025; a 10 -> 2 split on 2-Jun-2025 (x5). Zerodha holds 100 at an
    average ₹200 (₹20,000). Older units O satisfy (O + 10) x 5 = 100, so O = 10 (not 100 - 50 = 50), and they cost
    20,000 - 10,000 = ₹10,000 for 10 pre-split units = ₹1,000 each (₹200 after the split)."""
    from sqlalchemy import select

    from finresearch.db.models import PortfolioHolding, PortfolioLot
    from finresearch.portfolio.connectors.merge import merge_sync
    from finresearch.portfolio.importers import parse_tradebook
    from finresearch.portfolio.service import apply, manual_txn

    with db() as s:
        apply(s, parse_tradebook(_zerodha([("2025-01-10", "buy", 10, "1000", "S1")]), "z.csv"), filename="z.csv",
              sha256="c" * 64, saved_path=None)  # fmt: skip
        hold = s.scalar(select(PortfolioHolding).where(PortfolioHolding.isin == "INE000S01011"))
        manual_txn(
            s,
            {"holding_id": hold.id, "day": date(2025, 6, 2), "kind": "split", "meta": {"from": 10, "to": 2}},
        )
        h = BrokerHolding(
            name="EXSPLIT", quantity=D(100), isin="INE000S01011", symbol="EXSPLIT", avg_price=D(200)
        )
        res = merge_sync(s, account="Zerodha", source="zerodha_holdings", label="Zerodha", holdings=[h], trades=[],
                         today=TODAY, now=NOW)  # fmt: skip
        (b,) = res.baselines
        assert (D(b["units"]), D(b["avg_price"]), b["day"]) == (D(10), D(1000), "2025-01-09")
        assert _units(s, "Zerodha") == {"ISIN:INE000S01011": D(100)}
        lots = s.scalars(select(PortfolioLot).where(PortfolioLot.holding_id == hold.id)).all()
        assert sum(x.open_quantity * x.cost_per_unit for x in lots) == D(20000)


def test_an_ignored_baseline_does_not_swallow_later_trades(db):
    """A Zerodha holdings baseline on 30-Sep-2026, then a tradebook from 1-Aug-2026 (the baseline is now ignored by
    the lots: older history covers it). An API trade on 25-Sep-2026 is a real trade, not 'covered by the baseline'."""
    from finresearch.portfolio.connectors.merge import merge_sync
    from finresearch.portfolio.importers import parse_tradebook
    from finresearch.portfolio.service import apply

    with db() as s:
        h = BrokerHolding(
            name="EXSPLIT", quantity=D(10), isin="INE000S01011", symbol="EXSPLIT", avg_price=D(100)
        )
        merge_sync(s, account="Zerodha", source="zerodha_holdings", label="Zerodha", holdings=[h], trades=[],
                   today=TODAY, now=NOW)  # fmt: skip
        apply(s, parse_tradebook(_zerodha([("2026-08-01", "buy", 10, "100", "S1")]), "z.csv"), filename="z.csv",
              sha256="d" * 64, saved_path=None)  # fmt: skip
        assert _units(s, "Zerodha") == {"ISIN:INE000S01011": D(10)}  # the baseline is ignored, the buy stands
        t = BrokerTrade(day=date(2026, 9, 25), side="buy", quantity=D(2), price=D(110), name="EXSPLIT",
                        isin="INE000S01011", symbol="EXSPLIT", exchange="NSE", trade_id="A1", order_id="O1")  # fmt: skip
        res = merge_sync(s, account="Zerodha", source="zerodha_api", label="Zerodha", holdings=[], trades=[t],
                         today=TODAY, now=NOW)  # fmt: skip
        assert res.added == 1 and res.covered_by_baseline == 0
        assert _units(s, "Zerodha") == {"ISIN:INE000S01011": D(12)}


def test_an_older_statement_price_never_replaces_a_newer_one(db):
    from sqlalchemy import select

    from finresearch.db.models import PortfolioHolding
    from finresearch.portfolio.connectors.merge import merge_sync, remember_statement_prices

    with db() as s:
        h = BrokerHolding(name="EXSPLIT", quantity=D(10), isin="INE000S01011", symbol="EXSPLIT", avg_price=D(100),
                          last_price=D(150))  # fmt: skip
        merge_sync(s, account="Groww", source="groww_holdings", label="Groww", holdings=[h], trades=[], today=TODAY,
                   now=NOW)  # fmt: skip
        remember_statement_prices(s, account="Groww", holdings=[h], day=TODAY, label="Groww")
        old = BrokerHolding(name="EXSPLIT", quantity=D(10), isin="INE000S01011", symbol="EXSPLIT", avg_price=D(100),
                            last_price=D(90))  # fmt: skip
        remember_statement_prices(s, account="Groww", holdings=[old], day=date(2026, 6, 30), label="Groww")
        hold = s.scalar(select(PortfolioHolding).where(PortfolioHolding.isin == "INE000S01011"))
        assert hold.meta["statement_price"] == {"price": "150", "day": "2026-09-30", "source": "Groww"}


def test_trades_missed_by_a_partial_sync_are_read_again(db, monkeypatch):
    """Trades last read on 20-Sep-2026; the trades read then fails on 21..30-Sep (partial syncs, which still advance
    last_sync_day); on 1-Oct it works. It must ask from 20-Sep - 3 days overlap = 17-Sep, not 30-Sep - 3 = 27-Sep."""
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors import sync

    asked: list[date] = []

    class Fake:
        capabilities = frozenset({"holdings", "trades"})
        account, source, label = "Groww", "groww_api", "Groww"
        holdings_include_today, first_sync_days = True, 30
        fail = True

        def secret_values(self):
            return []

        async def holdings(self):
            return []

        positions = mf_holdings = funds = holdings

        async def trades(self, since, today):
            asked.append(since)
            if Fake.fail:
                raise base.ConnectorError("trades endpoint down")
            return []

    monkeypatch.setattr(sync, "build_from", lambda *cols: Fake())
    monkeypatch.setattr(sync, "token_valid", lambda row, now: True)
    with db() as s:
        s.add(BrokerConnection(key="groww", enabled=True, auto_sync=True, status="connected",
                               last_sync_day=date(2026, 9, 20), state={"trades_through": "2026-09-20"}))  # fmt: skip
    for d in (21, 30):
        assert run(sync.sync_now("groww", now=datetime(2026, 9, d, 11, 0, tzinfo=UTC)))["status"] == "partial"
    Fake.fail = False
    assert run(sync.sync_now("groww", now=datetime(2026, 10, 1, 11, 0, tzinfo=UTC)))["status"] == "ok"
    assert asked == [date(2026, 9, 17)] * 3
    run(sync.sync_now("groww", now=datetime(2026, 10, 2, 11, 0, tzinfo=UTC)))
    assert asked[-1] == date(2026, 9, 28)  # 1-Oct read them: from 1-Oct - 3 days


def test_inbox_password_is_tracked_by_a_random_revision_not_a_hash(db):
    """CodeQL py/weak-sensitive-data-hashing: a CAS password is usually PAN-derived, so even a truncated hash of it
    stored in the database could be brute-forced; the inbox keys failed files to a random revision id instead."""
    import hashlib

    from finresearch.portfolio.connectors.store import INBOX_KEY, update

    with db() as s:
        row = update(s, INBOX_KEY, {"config": {"password": "ABCDE1234F"}})
        rev1 = row.config["password_rev"]
        assert len(rev1) == 16 and hashlib.sha256(b"ABCDE1234F").hexdigest()[:12] not in rev1
        assert (
            update(s, INBOX_KEY, {"config": {"password": "ABCDE1234F"}}).config["password_rev"] == rev1
        )  # unchanged
        assert update(s, INBOX_KEY, {"config": {"password": "XYZAB9876C"}}).config["password_rev"] != rev1
        assert "password_rev" not in update(s, INBOX_KEY, {"config": {"clear_password": True}}).config


# ------------------------------------------------------------------ #266 broker sync hardening
def _groww_with_orders(orders):
    return {**GROWW, "/v1/order/list": {"status": "SUCCESS", "payload": {"order_list": orders}}}


def _order(oid, filled, avg, status="OPEN", **extra):
    return {"groww_order_id": oid, "trading_symbol": "NEWCO", "order_status": status, "quantity": 100,
            "filled_quantity": filled, "average_fill_price": avg, "exchange": "NSE", "transaction_type": "BUY",
            "created_at": "2026-09-30T10:00:00", "product": "CNC", **extra}  # fmt: skip


def _connect_groww(client):
    r = client.put("/api/connections/groww", headers=ORIGIN,
                   json={"config": {"api_key": GROWW_KEY, "totp_secret": SEED}, "auto_sync": True})  # fmt: skip
    assert r.status_code == 200, r.text


def test_an_order_synced_part_filled_then_filled_is_one_row(client, transport):
    """#266: 40 of 100 filled @100.5 at a pre-close Sync now, all 100 @100.3 at the after-close sync. Groww reports
    the order, not its fills, so this is one buy of 100 @100.3 (amount 10,030), never 140."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioTxn
    from finresearch.portfolio.connectors.sync import sync_now

    _connect_groww(client)
    transport(_groww_with_orders([_order("GO9", 40, "100.5")]))
    first = run(sync_now("groww", now=datetime(2026, 9, 30, 5, 30, tzinfo=UTC)))  # 11:00 IST
    assert first["status"] == "ok" and first["summary"]["added"] == 1
    transport(_groww_with_orders([_order("GO9", 100, "100.3", "EXECUTED")]))
    second = run(sync_now("groww", trigger="scheduled", now=NOW))  # 16:30 IST
    assert second["status"] == "ok" and second["summary"]["added"] == 0 and second["summary"]["updated"] == 1
    third = run(sync_now("groww", now=NOW))
    assert third["summary"]["updated"] == 0 and third["summary"]["duplicates"] == 1  # the same read again
    with session_scope() as s:
        rows = [t for t in s.scalars(select(PortfolioTxn).where(PortfolioTxn.source == "groww_api"))
                if (t.meta or {}).get("order_id") == "GO9"]  # fmt: skip
        assert [(r.quantity, r.price, r.amount) for r in rows] == [(D(100), D("100.3"), D("10030"))]
        assert _units(s, "Groww")["NSE:NEWCO"] == D(100)


def test_an_order_stored_twice_before_the_fix_is_a_conflict_not_a_guess(db):
    from finresearch.portfolio.connectors.merge import merge_sync
    from finresearch.portfolio.importers import ImportedTxn
    from finresearch.portfolio.service import add_txns, rebuild

    with db() as s:
        for q, p in (("40", "100.5"), ("100", "100.3")):  # what a sync before #266 left behind
            for hid in add_txns(s, [ImportedTxn(account="Groww", asset_type="stock", name="NEWCO", day=TODAY, kind="buy",
                                     quantity=D(q), price=D(p), amount=D(q) * D(p), source="groww_api",
                                     nse_symbol="NEWCO", meta={"order_id": "GO9"}, ext=q)]).holdings:  # fmt: skip
                rebuild(s, hid)
        t = _t(TODAY, "buy", "NEWCO", None, 100, "100.3", "GO9", "GO9")
        res = merge_sync(s, account="Groww", source="groww_api", label="Groww", holdings=[], trades=[t],
                         today=TODAY, now=NOW, order_keyed=True)  # fmt: skip
        assert res.added == 0 and res.updated == 0 and "stored 2 times" in res.conflicts[0]["why"]
        assert _units(s, "Groww")["NSE:NEWCO"] == D(140)  # left for the user to review, never edited blindly


def test_an_unexpected_failure_is_logged_and_not_retried_every_tick(client, transport, monkeypatch, caplog):
    """#266: the login works but storing the token fails (a locked Keychain): before, the exception escaped _finish,
    so no log row, no failed_day, and the next tick logged in again (Groww caps logins at 150 a day)."""
    from sqlalchemy import func, select

    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection, BrokerSyncLog
    from finresearch.portfolio.connectors import sync
    from finresearch.secrets import SecretStoreError

    _connect_groww(client)
    rec = transport(GROWW)

    def broken(s, key, grant):
        raise SecretStoreError(f"Keychain write failed for {grant.token}")

    monkeypatch.setattr(sync, "set_token", broken)
    out = run(sync.sync_now("groww", trigger="scheduled", now=NOW))
    assert out["status"] == "error" and "SecretStoreError" in out["error"] and ACCESS not in json.dumps(out)
    assert "SecretStoreError" in caplog.text and ACCESS not in caplog.text  # the log is redacted too
    with session_scope() as s:
        row = s.get(BrokerConnection, "groww")
        assert not sync.due(row, datetime(2026, 9, 30, 11, 1, tzinfo=UTC))  # no login on the next tick
        assert s.scalar(select(func.count()).select_from(BrokerSyncLog)) == 1
    assert len([r for r in rec.requests if r.method == "POST"]) == 1


def test_secret_reads_run_off_the_event_loop(client, transport, monkeypatch):
    import threading

    from finresearch.portfolio.connectors import store, sync

    _connect_groww(client)
    transport(GROWW)
    seen: list[bool] = []
    real = store.build_from

    def spy(*cols):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*cols)

    monkeypatch.setattr(sync, "build_from", spy)
    assert run(sync.sync_now("groww", now=NOW))["status"] == "ok"
    assert seen == [False]  # a Keychain prompt can no longer freeze the API


@pytest.mark.parametrize("body", [
    {"status": "SUCCESS", "payload": {"orders_v2": []}},  # the list renamed
    {"status": "SUCCESS"},  # no payload
    {"status": "SUCCESS", "payload": {"order_list": "x"}},
])  # fmt: skip
def test_a_changed_order_list_shape_is_a_failed_read_not_no_trades(client, transport, body):
    """#266: before, any of these read as "no orders today": status ok and trades_through moved to today, and the
    order list answers today only, so the day's trades were lost without a word."""
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors.sync import sync_now

    _connect_groww(client)
    transport({**GROWW, "/v1/order/list": body})
    out = run(sync_now("groww", now=NOW))
    assert out["status"] == "partial" and "unexpected response shape" in out["error"]
    assert out["summary"]["trades_since"] is None  # no claim of coverage
    with session_scope() as s:
        assert "trades_through" not in (s.get(BrokerConnection, "groww").state or {})


def test_an_empty_order_list_is_still_a_good_read(transport):
    from finresearch.portfolio.connectors.groww import GrowwConnector

    transport({**GROWW, "/v1/order/list": {"status": "SUCCESS", "payload": {"order_list": []}}})
    assert run(GrowwConnector({}, ACCESS).trades(TODAY, TODAY)) == []


def test_groww_funds_checks_the_status(transport):
    from finresearch.portfolio.connectors.base import ConnectorError
    from finresearch.portfolio.connectors.groww import GrowwConnector

    transport({**GROWW, "/v1/margins/detail/user": {"status": "FAILURE", "error": {"message": "down"},
                                                     "payload": {"clear_cash": 0}}})  # fmt: skip
    with pytest.raises(ConnectorError, match="down"):
        run(GrowwConnector({}, ACCESS).funds())  # before: {"cash": 0}, an unknown balance shown as ₹0


def test_a_failed_trades_read_is_retried_the_same_day_then_capped(client, transport):
    """#266: Groww's order list answers today only. A failed read at 16:30 IST is read again at +10, +20 and +40
    minutes; the 4th failure stops the day's scheduled attempts and says what to do."""
    from datetime import timedelta

    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors.sync import due, sync_now

    _connect_groww(client)
    transport({**GROWW, "/v1/order/list": {"status": "FAILURE", "error": {"message": "busy"}}})
    t, gaps = NOW, [10, 20, 40]
    for i in range(4):
        out = run(sync_now("groww", trigger="scheduled", now=t))
        assert out["status"] == "partial" and out["summary"]["trades_since"] is None
        with session_scope() as s:
            row = s.get(BrokerConnection, "groww")
            assert "trades_through" not in row.state
            if i < 3:
                nxt = t + timedelta(minutes=gaps[i])
                assert not due(row, nxt - timedelta(minutes=1)) and due(row, nxt)
                t = nxt
            else:
                assert "after 4 attempts" in out["error"] and row.state["failed_day"] == "2026-09-30"
                assert not due(row, t + timedelta(hours=2))
    transport(GROWW)  # the next day works again and clears the retry
    out = run(sync_now("groww", trigger="scheduled", now=datetime(2026, 10, 1, 11, 0, tzinfo=UTC)))
    # today-only: the read covers today, not "from 3 days before the last read"
    assert out["status"] == "ok" and out["summary"]["trades_since"] == "2026-10-01"


def test_another_process_holding_the_connection_blocks_sync_and_login(client, transport):
    from sqlalchemy import text

    from finresearch.db import get_engine
    from finresearch.portfolio.connectors.sync import SyncBusy, sync_now

    _connect_groww(client)
    rec = transport(GROWW)
    other = get_engine().connect()  # another process's sync
    try:
        assert other.execute(text("SELECT pg_try_advisory_lock(hashtext('broker_sync:groww'))")).scalar()
        other.commit()
        with pytest.raises(SyncBusy):
            run(sync_now("groww", now=NOW))
        assert client.post("/api/connections/groww/sync", headers=ORIGIN).status_code == 409
        assert client.post("/api/connections/groww/login", headers=ORIGIN).status_code == 409
        assert rec.requests == []  # no second login, no reads
    finally:
        other.execute(text("SELECT pg_advisory_unlock(hashtext('broker_sync:groww'))"))
        other.commit()
        other.close()
    assert run(sync_now("groww", now=NOW))["status"] == "ok"  # released: works again


def test_a_refused_token_is_cleared_only_if_it_is_still_the_one_used(client, transport):
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors.base import TokenGrant
    from finresearch.portfolio.connectors.store import set_token
    from finresearch.portfolio.connectors.sync import _finish

    _connect_groww(client)
    with session_scope() as s:
        old = set_token(s, "groww", TokenGrant("tok-old-123456", datetime(2026, 10, 1, 0, 30, tzinfo=UTC)))
    with session_scope() as s:
        new = set_token(s, "groww", TokenGrant("tok-new-123456", datetime(2026, 10, 1, 0, 30, tzinfo=UTC)))
    assert old != new
    _finish("groww", "manual", NOW, "reconnect", {}, "refused", today=None, expire=True, token_rev=old)
    with session_scope() as s:
        assert s.get(BrokerConnection, "groww").token  # a Log in after the sync started: its token stays
    _finish("groww", "manual", NOW, "reconnect", {}, "refused", today=None, expire=True, token_rev=new)
    with session_scope() as s:
        assert s.get(BrokerConnection, "groww").token is None


def test_groww_dates_a_trade_by_its_trade_date_not_order_creation():
    """An after-market order placed on the evening of 29-Sep fills on 30-Sep: `created_at` is the 29th, `trade_date`
    ("Date on which trade has taken place", order-list docs) the 30th."""
    from finresearch.portfolio.connectors.groww import map_order

    amo = _order("GO7", 5, 200, "EXECUTED", created_at="2026-09-29T19:30:00", exchange_time="2026-09-30T09:15:02",
                 trade_date="2026-09-30T09:15:02")  # fmt: skip
    assert map_order(amo).day == date(2026, 9, 30)
    del amo["trade_date"]
    assert map_order(amo).day == date(2026, 9, 30)  # exchange_time next
    del amo["exchange_time"]
    assert map_order(amo).day == date(2026, 9, 29)  # only the creation time is left (documented fallback)


def test_a_stock_bought_today_is_not_missing_at_the_broker(db):
    """Groww's holdings at 16:30 exclude today's buys: a new stock bought today is no reconciliation difference."""
    from finresearch.portfolio.connectors.merge import merge_sync

    with db() as s:
        res = merge_sync(s, account="Groww", source="groww_api", label="Groww",
                         holdings=[_h("INE000B01012", "EXBANK", 5, "480.5")],
                         trades=[_t(TODAY, "buy", "NEWCO", None, 3, 50, "GO5", "GO5")], today=TODAY, now=NOW,
                         holdings_include_today=False)  # fmt: skip
        assert res.added == 1 and all(r["ok"] for r in res.reconciliation), res.reconciliation


def test_differences_are_unknown_when_holdings_were_not_read(client, transport):
    from finresearch.portfolio.connectors.sync import sync_now

    _connect_groww(client)
    transport({**GROWW, "/v1/holdings/user": {"status": "FAILURE", "error": {"message": "down"}}})
    out = run(sync_now("groww", now=NOW))
    assert out["status"] == "partial"
    assert out["summary"]["reconciled"] is None and out["summary"]["differences"] is None  # not 0


def test_broker_schedule_uses_the_known_holiday_list(monkeypatch):
    """#266: a known holiday skips the after-close sync; an unknown list (not cached for the year) does not pause
    broker syncs (Groww's today-only order list cannot be fetched later), weekdays then count as trading days."""
    from finresearch.adapters import nse_holidays
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors import sync

    row = BrokerConnection(key="groww", enabled=True, auto_sync=True, status="connected", last_sync_day=None)
    at = datetime(2026, 9, 30, 16, 5, tzinfo=IST)
    monkeypatch.setattr(
        nse_holidays, "known_trading_holidays", lambda day, state_dir=None: {date(2026, 9, 30)}
    )
    assert not sync.due(row, at, sync._holidays(date(2026, 9, 30)))
    monkeypatch.setattr(nse_holidays, "known_trading_holidays", lambda day, state_dir=None: None)
    assert sync._holidays(date(2026, 9, 30)) == set() and sync.due(row, at, set())


def test_a_token_about_to_expire_counts_as_expired():
    from datetime import timedelta

    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors.store import token_valid

    row = BrokerConnection(key="groww", token="secret_ref:x", token_expires_at=NOW + timedelta(minutes=2))
    assert not token_valid(row, NOW)  # a sync now would fail half-way at 06:00 IST
    row.token_expires_at = NOW + timedelta(minutes=10)
    assert token_valid(row, NOW)


def test_groww_retries_a_refused_totp_once_with_the_next_step(monkeypatch):
    from finresearch.portfolio.connectors import groww
    from finresearch.portfolio.connectors.groww import GrowwConnector

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["totp"])
        if len(sent) == 1:
            return httpx.Response(401, json={"message": "invalid totp"})
        return httpx.Response(200, json={"token": ACCESS})

    monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(groww.time, "time", lambda: 1_790_000_029.0)  # 1 s before a step turns
    assert run(GrowwConnector({"api_key": GROWW_KEY, "totp_secret": SEED}).login()).token == ACCESS
    assert sent == [totp(SEED, 1_790_000_029.0), totp(SEED, 1_790_000_059.0)] and sent[0] != sent[1]

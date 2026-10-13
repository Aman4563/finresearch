"""#316: the SGB redemption exemption after 1-Apr-2026 (original subscriber, maturity or premature RBI redemption,
never assumed), the listed treatment of G-secs/SDLs/T-bills, and Groww's basket margin calculator as the one
read-only POST. Synthetic instruments only (made-up names, fake ISINs). Expected values are worked out by hand."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal as D

from finresearch.fincalc.tax import classify
from finresearch.portfolio.tax import auto_tax_class, holding_subscriber, lot_subscription


# --------------------------------------------------------------------------- 1. SGB: who subscribed
def test_lot_subscription_from_the_acquiring_transaction():
    assert lot_subscription("buy", {"sgb_subscription": True}) is True  # entered as bought at issue
    assert lot_subscription("buy", {"sgb_subscription": False}) is False
    # a broker tradebook/API trade: bought on the exchange (secondary market)
    assert lot_subscription("buy", {"exchange": "NSE", "trade_id": "T1"}) is False
    assert lot_subscription("buy", {"order_id": "O1"}) is False
    # a holdings-sync opening carries the listing exchange, which proves nothing; a bare manual buy neither
    assert lot_subscription("opening", {"exchange": "NSE", "statement_opening": True}) is None
    assert lot_subscription("buy", {}) is None and lot_subscription("buy", None) is None


def test_holding_setting_is_three_way_and_the_old_unticked_checkbox_is_not_an_answer():
    assert holding_subscriber({"sgb_subscribed": True}) is True
    assert holding_subscriber({"sgb_subscribed": False}) is False
    assert holding_subscriber({}) is None and holding_subscriber(None) is None
    assert holding_subscriber({"sgb_original_subscriber": True}) is True
    assert holding_subscriber({"sgb_original_subscriber": False}) is None  # saved by every unticked save


def test_sgb_paths_after_apr_2026():
    sub, maturity, early = date(2018, 11, 20), date(2026, 11, 20), date(2026, 5, 20)
    for sold in (maturity, early):  # maturity and premature RBI redemption (after year 5) alike
        assert classify("sgb", sub, sold, rbi_redemption=True, original_subscriber=True).term == "exempt"
        sec = classify("sgb", sub, sold, rbi_redemption=True, original_subscriber=False)
        assert (sec.term, sec.rate, sec.rule.id) == ("long", D("0.125"), "other-listed-2024")
        assert classify("sgb", sub, sold, rbi_redemption=True).term == "unknown"
    # an exchange sale is taxable even for the original subscriber; 12-month listed rule
    assert classify("sgb", sub, early, original_subscriber=True).term == "long"
    short = classify("sgb", date(2025, 6, 1), date(2026, 6, 1), original_subscriber=True)  # exactly 12 months
    assert (short.term, short.rate) == ("short", None)


# the portfolio API fixture (TODAY there is 2026-09-30); tests taking it carry a noqa for F811
from test_portfolio import ORIGIN, client  # noqa: E402,F401

SGB = {"asset_type": "stock", "name": "SGB 2018-19 SERIES XX", "nse_symbol": "SGBXX26", "account": "Manual",
       "day": "2018-05-14", "kind": "buy", "quantity": "10", "price": "3100", "stt_paid": False}  # fmt: skip


def _redeem(c, hid, meta=None):
    # premature RBI redemption after the fifth year, 14-May-2026 (tax year 2026-27): 10 @ 9,800
    r = c.post("/api/portfolio/transactions", headers=ORIGIN,
               json={"holding_id": hid, "day": "2026-05-14", "kind": "redemption", "quantity": "10",
                     "price": "9800", "stt_paid": False, "meta": meta or {}})  # fmt: skip
    assert r.status_code < 300, r.text


def _tax(c):
    tax = c.get("/api/portfolio/tax").json()
    (d,) = tax["disposals"]
    return d, next(x for x in tax["fys"] if x["fy"] == 2027)


def _set(c, hid, v):
    r = c.put(
        f"/api/portfolio/holdings/{hid}", headers=ORIGIN, json={"flags": {"sgb_original_subscriber": v}}
    )
    assert r.status_code == 200, r.text


def test_sgb_premature_redemption_through_the_api(client):  # noqa: F811
    hid = client.post("/api/portfolio/transactions", headers=ORIGIN, json=SGB).json()["holding_id"]
    _redeem(client, hid, {"held_to_maturity": False})
    h = next(x for x in client.get("/api/portfolio").json()["holdings"] if x["id"] == hid)
    assert h["sgb_original_subscriber"] is None
    d, fy = _tax(client)  # not set: unknown, the year incomplete (never exempt, never a guessed figure)
    assert d["term"] == "unknown" and "subscribed at issue" in d["reason"] and fy["complete"] is False
    _set(client, hid, True)  # subscribed at issue: exempt; 10 x (9,800 - 3,100) = 67,000 exempt
    d, fy = _tax(client)
    assert (d["term"], d["rule"], fy["exempt"], fy["total"]) == (
        "exempt",
        "sgb-redemption-2026",
        67000.0,
        0.0,
    )
    _set(client, hid, False)  # bought on the exchange: LTCG 67,000 x 12.5 % = 8,375 + 4 % cess 335 = 8,710
    d, fy = _tax(client)
    assert (d["term"], d["rate_pct"], fy["total"]) == ("long", 12.5, 8710.0)
    _set(client, hid, None)  # cleared: unknown again
    assert _tax(client)[0]["term"] == "unknown"


def test_sgb_exchange_bought_lot_overrides_the_holding_setting(client):  # noqa: F811
    hid = client.post("/api/portfolio/transactions", headers=ORIGIN,
                      json={**SGB, "meta": {"sgb_subscription": False}}).json()["holding_id"]  # fmt: skip
    _set(client, hid, True)
    _redeem(client, hid)
    d, fy = _tax(client)
    assert (d["term"], fy["total"]) == ("long", 8710.0)


# --------------------------------------------------------------------------- 2. G-secs, SDLs, T-bills
def test_government_securities_are_listed_with_the_12_month_rule():
    # s.2(42A) first proviso (Finance (No.2) Act 2024): listed security, long-term after more than 12 months;
    # s.112 12.5 % for transfers from 23-Jul-2024, no s.112A exemption
    for isin, name, sym in (("IN0020230085", "7.18% GOI 2033", "718GS2033"),
                            ("IN1920240012", "7.25% Example SDL 2034", None),
                            ("IN002025Z123", "364 DAY TBILL 14-MAY-2026", None)):  # fmt: skip
        cls, why = auto_tax_class("stock", name, None, None, sym, isin)
        assert cls == "other", why
        exact = classify(cls, date(2025, 3, 10), date(2026, 3, 10), listed=True, debt=True)
        assert (exact.term, exact.rate) == ("short", None)  # held exactly 12 months: short at slab
        later = classify(cls, date(2025, 3, 10), date(2026, 3, 11), listed=True, debt=True)
        assert (later.term, later.rate, later.rule.id) == ("long", D("0.125"), "other-listed-2024")
    assert re.search(
        r"s\.2\(42A\)", auto_tax_class("stock", "7.18% GOI 2033", None, None, None, "IN0020230085")[1]
    )


# --------------------------------------------------------------------------- 3. Groww basket margin calculator
# the F&O API fixture (fake Groww market and sources, TRADE_DAY 12-Oct-2026)
from test_fno_book import api, book_db, offline  # noqa: E402,F401

ORDER_WORDS = ("/order/create", "/order/modify", "/order/cancel", "/order/smart/create", "/order/smart/cancel",
               "/order/oco/create", "/positions/user", "/margins/detail/user")  # fmt: skip


def _all_lists():
    from finresearch.adapters import groww_market as gm
    from finresearch.portfolio.connectors import groww

    return {"READ_PATHS": groww.READ_PATHS, "AUTH_PATHS": groww.AUTH_PATHS, "MARKET_PATHS": gm.MARKET_PATHS,
            "ASSET_PATHS": gm.ASSET_PATHS, "CALC_PATHS": gm.CALC_PATHS}  # fmt: skip


def test_the_margin_calculator_is_the_only_non_get_non_login_path_and_no_order_path_is_reachable():
    from finresearch.adapters import groww_market as gm
    from finresearch.portfolio.connectors import groww

    lists = _all_lists()
    assert gm.CALC_PATHS == (r"/margins/detail/orders",)  # exactly one entry
    # every regex in every list, against order/modify/cancel paths: nothing matches
    for name, paths in lists.items():
        for p in paths:
            for bad in ("/order/create", "/order/modify", "/order/cancel", "/order/smart/create"):
                assert not re.fullmatch(p, bad), (name, p, bad)
    # the calculator path is in no other list (so no GET or login POST client reaches it)
    for name, paths in lists.items():
        if name != "CALC_PATHS":
            assert not any(re.fullmatch(p, "/margins/detail/orders") for p in paths), name
    # the one client that carries CALC_PATHS: POST to the calculator only, through calc_post only
    http = gm.GrowwMarket()._calc_api("tok")
    http._check("POST", "/margins/detail/orders", "calc")
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        _forbidden(http, method, "/margins/detail/orders", "calc")
    for path in ORDER_WORDS:
        for method in ("GET", "POST", "PUT", "DELETE"):
            _forbidden(http, method, path, "calc")
            _forbidden(http, method, path, None)
    _forbidden(http, "POST", "/margins/detail/orders", None)  # auth_post cannot reach it
    _forbidden(http, "POST", "/token/api/access", "calc")  # calc_post cannot reach login
    # the connector and market clients stay GET-only (+ login)
    for paths in (groww.READ_PATHS, gm.MARKET_PATHS):
        from finresearch.portfolio.connectors.base import ReadOnlyHttp

        c = ReadOnlyHttp(groww.BASE, read_paths=paths, auth_paths=groww.AUTH_PATHS)
        _forbidden(c, "POST", "/margins/detail/orders", "calc")


def _forbidden(http, method, path, kind):
    import pytest

    from finresearch.portfolio.connectors.base import ForbiddenRequest

    with pytest.raises(ForbiddenRequest):
        http._check(method, path, kind)


def test_margin_counts_against_the_non_trading_budget():
    from finresearch.adapters.groww_budget import LABELS, category

    # docs rate-limit table: "Non Trading: Order Status, Order list, Trade list, Positions, Holdings, Margin"
    assert category("/margins/detail/orders") == "non_trading" and "margin" in LABELS["non_trading"]


def _fake_groww(monkeypatch, payload):
    """A transport for api.groww.in: records the request and answers `payload` for the calculator."""
    import httpx

    from finresearch.adapters import groww_budget
    from finresearch.portfolio.connectors import base

    seen = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(groww_budget.BUDGET, "wait_s", lambda cat: 0.0)
    return seen


def test_basket_margin_request_and_answer(monkeypatch):
    import asyncio
    import json

    from test_fno_book import EXPIRY, FakeMarket

    from finresearch.adapters import groww_market as gm

    inst = FakeMarket().inst
    orders = gm.basket_orders(inst, "NIFTY", EXPIRY, [("call", 25000.0, "buy", 75, 119.0),
                                                      ("call", 25200.0, "sell", 75, None)])  # fmt: skip
    assert orders == [
        {"trading_symbol": "NIFTY26OCT25000CE", "transaction_type": "BUY", "quantity": 75, "order_type": "LIMIT",
         "price": 119.0, "product": "NRML", "exchange": "NSE"},
        {"trading_symbol": "NIFTY26OCT25200CE", "transaction_type": "SELL", "quantity": 75, "order_type": "MARKET",
         "product": "NRML", "exchange": "NSE"}]  # fmt: skip
    seen = _fake_groww(monkeypatch, {"status": "SUCCESS", "payload": {
        "exposure_required": 28125.5, "span_required": 61000, "option_buy_premium": 0, "brokerage_and_charges": 40,
        "total_requirement": 89165.5, "cash_cnc_margin_required": 0}})  # fmt: skip
    m = gm.GrowwMarket()
    monkeypatch.setattr(m, "token", lambda: "tok-123456")
    got = asyncio.run(m.basket_margin(orders))
    assert got == {"span_required": 61000.0, "exposure_required": 28125.5, "option_buy_premium": 0.0,
                   "brokerage_and_charges": 40.0, "total_requirement": 89165.5}  # fmt: skip
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/v1/margins/detail/orders"
    assert req.url.params["segment"] == "FNO" and json.loads(req.content) == orders
    assert req.headers["X-API-VERSION"] == "1.0" and req.headers["Authorization"] == "Bearer tok-123456"


def test_margin_api_uses_groww_and_falls_back_labelled(monkeypatch, api):  # noqa: F811
    from test_fno_book import SPREAD, TRADE_DAY, at_ist

    from finresearch.adapters import groww_market as gm

    body = {k: SPREAD[k] for k in ("symbol", "expiry", "legs")}
    sent = []

    async def ok(orders, segment="FNO"):
        sent.append((segment, orders))
        return {"span_required": 61000.0, "exposure_required": 28125.5, "option_buy_premium": 8925.0,
                "brokerage_and_charges": 40.0, "total_requirement": 98090.5}  # fmt: skip

    monkeypatch.setattr(gm.MARKET, "basket_margin", ok, raising=False)
    with api(at_ist(TRADE_DAY, 10)) as c:
        r = c.post("/api/fno/margin", json=body).json()
    g = r["groww_calculator"]
    assert r["margin_source"] == "groww" and g["available"] and g["total"] == 98090.5 and g["span"] == 61000.0
    ((seg, orders),) = sent
    assert seg == "FNO" and [o["trading_symbol"] for o in orders] == [
        "NIFTY26OCT25000CE",
        "NIFTY26OCT25200CE",
    ]
    assert [o["quantity"] for o in orders] == [75, 75]  # 1 lot x 75
    assert r["status"] == "approximate" and r["total"] > 0  # the approximation is still there, labelled

    async def down(orders, segment="FNO"):
        from finresearch.portfolio.connectors.base import ConnectorError

        raise ConnectorError("HTTP 503 from /margins/detail/orders: busy")

    monkeypatch.setattr(gm.MARKET, "basket_margin", down, raising=False)
    with api(at_ist(TRADE_DAY, 10)) as c:
        r = c.post("/api/fno/margin", json=body).json()
    assert r["margin_source"] == "approximate" and r["groww_calculator"]["available"] is False
    assert "HTTP 503" in r["groww_calculator"]["reason"] and "fallback" in r["groww_calculator"]["reason"]
    assert "approximate" in r["label"] and r["total"] > 0

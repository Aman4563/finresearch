"""The forward quote recorder (#363, P1.1): Groww's Get Quote on the read-only allow-list, contract selection, parsing,
per-row and per-sweep data-quality checks, storage, the claimed monitor slots and the NSE fallback. Every Groww
response is synthetic, shaped after the Get Quote sample in Groww's docs (adapters.groww_market docstring); no
network, no real account, no real token."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest

from finresearch.adapters import groww_budget, groww_market
from finresearch.adapters.groww_market import GrowwMarket
from finresearch.fincalc.dates import IST
from finresearch.portfolio.connectors import base
from finresearch.portfolio.connectors.base import ForbiddenRequest

TOKEN = "-".join(["fake", "access"]) + "q" * 20  # assembled at runtime: no key-like literal (gitleaks)
TUE = date(2026, 10, 20)  # a Tuesday, no NSE holiday
AT_0930 = datetime(2026, 10, 20, 9, 30, 5, tzinfo=IST)


def quote_payload(bid=120.05, ask=121.0, ltt_ms=None, depth=True) -> dict:
    """The documented Get Quote payload (field names from Groww's docs, values made up)."""
    p = {"average_price": 120.4, "bid_price": bid, "bid_quantity": 750, "offer_price": ask, "offer_quantity": 1500,
         "last_price": 120.5, "last_trade_quantity": 75, "last_trade_time": ltt_ms, "volume": 123450,
         "open_interest": 456000, "previous_open_interest": 450000, "implied_volatility": 12.34,
         "total_buy_quantity": 90000, "total_sell_quantity": 80000, "ohlc": "{open: 1,high: 2,low: 1,close: 1}"}  # fmt: skip
    if depth:
        p["depth"] = {"buy": [{"price": bid, "quantity": 750}, {"price": 120.0, "quantity": 300},
                              {"price": 0, "quantity": 0}],
                      "sell": [{"price": ask, "quantity": 1500}, {"price": 121.5, "quantity": 225}]}  # fmt: skip
    return p


class FakeSession:
    def __init__(self, token: str | None = TOKEN) -> None:
        self._token, self.state = token, "connected" if token else "not_connected"

    def token(self) -> str | None:
        return self._token

    def forget(self) -> None:
        self._token = None


@pytest.fixture
def mock_groww(monkeypatch):
    """A MockTransport answering /v1/live-data/quote; returns the list of requests."""

    class NoWait:
        def reserve(self, key, interval):
            return 0.0

        def hold_off(self, key, seconds):
            pass

    monkeypatch.setattr(groww_budget.BUDGET, "slots", NoWait())
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/live-data/quote":
            return httpx.Response(200, json={"status": "SUCCESS", "payload": quote_payload()})
        return httpx.Response(404, json={"status": "FAILURE", "error": {"message": "no route"}})

    monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(handler))
    return seen


def market(t: datetime = AT_0930, token: str | None = TOKEN) -> GrowwMarket:
    return GrowwMarket(session=FakeSession(token), clock=lambda: t.astimezone(UTC), holidays=set)


# ------------------------------------------------------------------ the Get Quote endpoint, read-only
def test_get_quote_is_an_allowed_get_in_the_live_bucket(mock_groww):
    m = market()
    payload = asyncio.run(m.quote("NIFTY26OCT25000CE"))
    assert payload["bid_price"] == 120.05 and payload["offer_quantity"] == 1500
    (r,) = mock_groww
    assert r.method == "GET" and r.url.path == "/v1/live-data/quote"
    assert dict(r.url.params) == {"exchange": "NSE", "segment": "FNO", "trading_symbol": "NIFTY26OCT25000CE"}
    assert groww_budget.category("/live-data/quote") == "live"
    with pytest.raises(ForbiddenRequest):  # still GET only
        asyncio.run(m._api(TOKEN).auth_post("/live-data/quote", json={}))


def test_get_quote_is_never_cached(mock_groww):
    m = market()
    asyncio.run(m.quote("NIFTY26OCT25000CE"))
    asyncio.run(m.quote("NIFTY26OCT25000CE"))
    assert len(mock_groww) == 2  # each call is a snapshot


def test_get_quote_without_a_session_raises(mock_groww):
    from finresearch.portfolio.connectors.base import ConnectorError

    with pytest.raises(ConnectorError):
        asyncio.run(market(token=None).quote("NIFTY26OCT25000CE"))
    assert mock_groww == []

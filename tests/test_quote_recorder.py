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


# ------------------------------------------------------------------ parsing: paise, unknown stays None
def test_parse_groww_quote_to_compact_columns():
    from finresearch.fnohist import quotes as q

    ltt = int(datetime(2026, 10, 20, 9, 29, 58, tzinfo=IST).timestamp() * 1000)
    row = q.parse_groww(quote_payload(ltt_ms=ltt))
    assert (row["bid"], row["bid_qty"], row["ask"], row["ask_qty"]) == (12005, 750, 12100, 1500)
    assert row["ltp"] == 12050 and row["oi"] == 456000 and row["volume"] == 123450
    assert row["iv"] == pytest.approx(12.34)
    # depth flattened as (price paise, quantity) pairs in the order returned; the empty level (price 0) is dropped
    assert row["depth_bid"] == [12005, 750, 12000, 300] and row["depth_ask"] == [12100, 1500, 12150, 225]
    assert row["exchange_at"] == datetime(2026, 10, 20, 9, 29, 58, tzinfo=IST)


def test_parse_groww_quote_missing_sides_are_unknown_not_zero():
    from finresearch.fnohist import quotes as q

    p = quote_payload(bid=0, ask=None, depth=False)
    p.update(bid_quantity=0, offer_quantity=None, implied_volatility=0, last_price=None, open_interest=None)
    row = q.parse_groww(p)
    assert row["bid"] is None and row["bid_qty"] is None  # a zero bid is "no bid", never a bid of 0
    assert row["ask"] is None and row["ask_qty"] is None
    assert row["depth_bid"] is None and row["depth_ask"] is None
    assert row["iv"] is None and row["ltp"] is None and row["oi"] is None and row["exchange_at"] is None
    assert row["volume"] == 123450


def test_row_flags():
    from finresearch.fnohist import quotes as q

    t = datetime(2026, 10, 20, 9, 30, 10, tzinfo=IST)
    fresh = t - timedelta(seconds=60)
    assert q.flags({"bid": 100, "ask": 105, "exchange_at": fresh}, t) == 0
    assert q.flags({"bid": 105, "ask": 105, "exchange_at": fresh}, t) == q.LOCKED
    assert q.flags({"bid": 106, "ask": 105, "exchange_at": fresh}, t) == q.CROSSED
    assert q.flags({"bid": None, "ask": 105, "exchange_at": fresh}, t) == q.NO_BID
    assert q.flags({"bid": 100, "ask": None, "exchange_at": None}, t) == q.NO_ASK | q.NO_TRADE_TIME
    assert q.flags({"bid": 100, "ask": 105, "exchange_at": fresh - timedelta(seconds=1)}, t) == q.STALE


# ------------------------------------------------------------------ the cost model's interface (issue maths)
def test_mid_half_spread_and_depth_cost_by_hand():
    from finresearch.fnohist.quotes import Quote

    qt = Quote.from_row({"bid": 12005, "ask": 12100, "bid_qty": 750, "ask_qty": 1500,
                         "depth_bid": [12005, 750, 12000, 300], "depth_ask": [12100, 1500, 12150, 225]})  # fmt: skip
    assert qt.mid == Decimal("120.525") and qt.half_spread == Decimal("0.475")
    assert qt.rel_half_spread == Decimal("0.475") / Decimal("120.525")
    # buy 1600: 121.00 x 1500 + 121.50 x 100 - 1600 x 120.525 = 193650 - 192840 = 810
    assert qt.depth_cost(1600, "buy") == Decimal("810.000")
    # sell 900: 900 x 120.525 - (120.05 x 750 + 120.00 x 150) = 108472.5 - 108037.5 = 435
    assert qt.depth_cost(900, "sell") == Decimal("435.000")
    assert qt.depth_cost(1800, "buy") is None  # deeper than the book shown: unknown, not the last level's price


def test_no_mid_without_a_two_sided_uncrossed_quote():
    from finresearch.fnohist.quotes import Quote

    for bid, ask in ((None, 100), (100, None), (100, 100), (101, 100)):
        qt = Quote.from_row({"bid": bid, "ask": ask})
        assert qt.mid is None and qt.half_spread is None and qt.rel_half_spread is None
        assert qt.depth_cost(1, "buy") is None

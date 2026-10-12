"""F&O positions, paper trading, exit rules, margin approximation and the event strip (#308). Fixtures only: synthetic
Groww answers in the docs' shapes, made-up contracts and prices; no network."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D

import httpx
import pytest

from finresearch.portfolio.connectors import base

ACCESS = "access-token-synthetic-fno-0000000000"

# docs-sample shapes (https://groww.in/trade-api/docs/curl/portfolio, read 12-Oct-2026); made-up contracts
FNO_ROWS = [
    # long 75 calls: bought 75 today, nothing sold -> direction confirmed
    {"trading_symbol": "NIFTY26OCT25000CE", "credit_quantity": 75, "credit_price": 120, "debit_quantity": 0,
     "debit_price": 0, "carry_forward_credit_quantity": 0, "carry_forward_credit_price": 0,
     "carry_forward_debit_quantity": 0, "carry_forward_debit_price": 0, "exchange": "NSE", "quantity": 75,
     "product": "NRML", "net_price": 120, "realised_pnl": 0},
    # short 75 puts carried forward, quantity negative -> confirmed short
    {"trading_symbol": "NIFTY26OCT24500PE", "credit_quantity": 0, "debit_quantity": 0,
     "carry_forward_credit_quantity": 0, "carry_forward_debit_quantity": 75, "exchange": "NSE", "quantity": -75,
     "product": "NRML", "net_price": "80.5", "realised_pnl": 0},
    # sold 75 but quantity +75: unsigned? -> unknown, never guessed
    {"trading_symbol": "NIFTY26OCT25500CE", "credit_quantity": 0, "debit_quantity": 75,
     "carry_forward_credit_quantity": 0, "carry_forward_debit_quantity": 0, "exchange": "NSE", "quantity": 75,
     "product": "NRML", "net_price": 40, "realised_pnl": 0},
    # no credit/debit fields at all -> unknown
    {"trading_symbol": "NIFTY26OCT24000PE", "exchange": "NSE", "quantity": 75, "product": "NRML", "net_price": 30},
    # closed today: dropped
    {"trading_symbol": "NIFTY26OCT24800PE", "credit_quantity": 75, "debit_quantity": 75,
     "carry_forward_credit_quantity": 0, "carry_forward_debit_quantity": 0, "quantity": 0, "net_price": 0,
     "realised_pnl": 512.5},
]  # fmt: skip


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def groww_fno(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/positions/user" and request.url.params.get("segment") == "FNO":
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"positions": FNO_ROWS}})
        return httpx.Response(404, json={"message": "no route"})

    monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(handler))
    return seen


# ------------------------------------------------------------------ 1. positions parsing
def test_fno_positions_parse_with_direction_only_when_confirmed(groww_fno):
    from finresearch.portfolio.connectors.groww import GrowwConnector

    c = GrowwConnector({"api_key": "k", "totp_secret": "JBSWY3DPEHPK3PXP"}, ACCESS)
    got = run(c.fno_positions())
    assert [(p.symbol, p.quantity, p.direction_known) for p in got] == [
        ("NIFTY26OCT25000CE", D(75), True), ("NIFTY26OCT24500PE", D(-75), True),
        ("NIFTY26OCT25500CE", D(75), False), ("NIFTY26OCT24000PE", D(75), False)]  # fmt: skip
    assert got[1].avg_price == D("80.5") and got[1].segment == "FNO" and got[1].sell_quantity == D(75)
    assert "unknown" in got[2].note and "lacks" in got[3].note
    (req,) = groww_fno
    assert req.method == "GET" and req.url.params["segment"] == "FNO"


def test_allow_lists_still_block_writes_and_order_paths():
    from finresearch.adapters import groww_market as gm
    from finresearch.portfolio.connectors import groww
    from finresearch.portfolio.connectors.base import ForbiddenRequest, ReadOnlyHttp

    for paths in (groww.READ_PATHS, gm.MARKET_PATHS):
        http = ReadOnlyHttp(groww.BASE, read_paths=paths, auth_paths=groww.AUTH_PATHS)
        for method, path in [("POST", "/margins/detail/orders"), ("POST", "/order/create"), ("GET", "/order/create"),
                             ("POST", "/positions/user"), ("PUT", "/order/modify"), ("DELETE", "/order/cancel"),
                             ("GET", "/order/smart/create"), ("POST", "/live-data/ltp")]:  # fmt: skip
            with pytest.raises(ForbiddenRequest):
                http._check(method, path)
        assert not any(__import__("re").fullmatch(p, "/margins/detail/orders") for p in paths)

"""Concurrent, cached price fetching for the portfolio (fake clients with delays; no network)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from finresearch.adapters.amfi import SchemeNav
from finresearch.adapters.nse import Quote
from finresearch.api.markets import MarketSources
from finresearch.portfolio.valuation import fetch_prices

DELAY = 0.2
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}


@dataclass
class H:
    id: int
    asset_type: str
    name: str
    nse_symbol: str | None = None
    bse_code: str | None = None
    isin: str | None = None
    scheme_code: str | None = None
    meta: dict = field(default_factory=dict)


class Fakes:
    def __init__(self) -> None:
        self.quotes: list[str] = []
        self.navall = 0
        self.live = self.peak = 0

    async def quote(self, sym: str, exch: str = "NSE") -> Quote:
        self.quotes.append(sym)
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(DELAY)
        self.live -= 1
        return Quote(symbol=sym, last_price=D(100))

    async def rows(self) -> list:
        self.navall += 1
        await asyncio.sleep(DELAY)
        return [SchemeNav(str(c), f"Fund {c}", "Direct", "Growth", None, None, D(50), date(2026, 9, 29),
                          "Equity Scheme - Flexi Cap Fund", "X") for c in range(900, 905)]  # fmt: skip


def holdings(n_stocks: int, n_funds: int) -> list[H]:
    hs = [H(i, "stock", f"S{i}", nse_symbol=f"SYM{i}") for i in range(n_stocks)]
    hs.append(H(100, "stock", "S0 in a second account", nse_symbol="SYM0"))  # same instrument: one quote
    hs += [H(200 + i, "mf", f"F{i}", scheme_code=str(900 + i)) for i in range(n_funds)]
    return hs


async def test_n_holdings_make_one_request_per_instrument_and_run_concurrently():
    f = Fakes()
    seen: list[int] = []
    t = time.perf_counter()
    out = await fetch_prices(holdings(12, 5), quote=f.quote, scheme_rows=f.rows, on_price=lambda hid, _p: seen.append(hid),
                             concurrency=4)  # fmt: skip
    took = time.perf_counter() - t
    assert len(out) == 18 and all(p.price is not None for p in out.values())
    assert sorted(f.quotes) == sorted(f"SYM{i}" for i in range(12))  # 12 quotes for 13 stock holdings
    assert f.navall == 1  # one NAVAll download prices all 5 funds
    assert f.peak == 4  # bounded concurrency, and actually concurrent
    # serial would be (12 + 1) × 0.2 = 2.6 s; 12 quotes 4 at a time = 3 rounds ≈ 0.6 s, NAVAll overlapping
    assert took < 1.2, took
    assert sorted(seen) == sorted(out)  # every price was announced as it arrived


async def test_a_failed_quote_degrades_only_its_rows():
    f = Fakes()

    async def flaky(sym: str, exch: str = "NSE"):
        if sym == "SYM1":
            raise RuntimeError("NSE HTTP 503")
        return await f.quote(sym, exch)

    out = await fetch_prices(holdings(3, 0), quote=flaky, scheme_rows=None)
    assert out[1].price is None and "RuntimeError" in out[1].error and out[2].price == D(100)


async def test_bse_fallback_numeric_codes_and_broker_statement_price():
    asked: list[tuple[str, str]] = []

    async def quote(sym: str, exch: str = "NSE") -> Quote:
        asked.append((sym, exch))
        if exch == "NSE":  # NSE knows the symbols but has no trades in them
            return Quote(symbol=sym, last_price=None)
        return Quote(symbol=sym, last_price=D(52))

    hs = [
        H(1, "stock", "BSE-traded", nse_symbol="AMDX", bse_code="532828"),  # NSE has no price: BSE prices it
        H(2, "stock", "NCD", nse_symbol="941149.0"),  # a BSE code stored as the symbol: quoted on BSE
        H(3, "stock", "Unlisted", nse_symbol="NSE$",
          meta={"statement_price": {"price": "1763.05", "day": "2026-09-30", "source": "Groww"}}),
    ]  # fmt: skip
    out = await fetch_prices(hs, quote=quote, scheme_rows=None)
    assert out[1].price == D(52) and out[1].source.startswith("BSE")
    assert out[2].price == D(52) and ("941149", "BSE") in asked and ("941149.0", "NSE") not in asked
    assert (
        out[3].price == D("1763.05")
        and out[3].source == "Groww statement close"
        and out[3].as_of == "2026-09-30"
    )
    assert out[3].error  # the reason the live price is missing stays visible


@pytest.fixture
def client(env):
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                       "portfolio_snapshot, portfolio_setting"))  # fmt: skip
    f = Fakes()
    app = create_app(nav_all=f.rows)
    app.state.markets = MarketSources(quote=f.quote, today=lambda: date(2026, 9, 30))
    with TestClient(app) as c:
        for i in range(8):
            r = c.post("/api/portfolio/transactions", headers=ORIGIN,
                       json={"asset_type": "stock", "name": f"S{i}", "nse_symbol": f"SYM{i}", "day": "2025-01-02",
                             "kind": "buy", "quantity": "10", "price": "90"})  # fmt: skip
            assert r.status_code == 201
        c.post("/api/portfolio/transactions", headers=ORIGIN,
               json={"asset_type": "mf", "name": "F0", "scheme_code": "900", "day": "2025-01-02", "kind": "buy",
                     "quantity": "10", "price": "40"})  # fmt: skip
        yield c, f


def test_cached_view_first_then_stream_then_cache_reuse(client):
    c, f = client
    t = time.perf_counter()
    first = c.get("/api/portfolio?prices=cached").json()
    assert time.perf_counter() - t < 0.5 and f.quotes == [] and f.navall == 0  # no network at all
    assert first["pending"] == 9 and all(h["pending"] and h["cost"] for h in first["holdings"])

    t = time.perf_counter()
    with c.stream("GET", "/api/portfolio/prices/stream") as r:
        events = [json.loads(line) for line in r.iter_lines() if line]
    took = time.perf_counter() - t
    prices = [e for e in events if e["type"] == "price"]
    assert (
        len(prices) == 9
        and events[-1]["type"] == "done"
        and sorted(e["price"] for e in prices) == [50] + [100] * 8
    )
    assert len(f.quotes) == 8 and f.navall == 1 and f.peak > 1 and took < 1.2, (f.peak, took)

    full = c.get("/api/portfolio").json()  # everything is cached now: no new requests
    assert full["pending"] == 0 and full["complete"] and len(f.quotes) == 8 and f.navall == 1
    assert full["summary"]["value"] == 8 * 10 * 100 + 10 * 50
    again = c.get("/api/portfolio?prices=cached").json()
    assert again["pending"] == 0 and len(f.quotes) == 8

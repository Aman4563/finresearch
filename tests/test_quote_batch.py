"""The portfolio's live quote path stays fast and bounded on a cold NSE session (recorded NSE bodies, no network).

Regression: a first /api/portfolio load took over 120 s (reproduced 1-Oct-2026 on a test server with 21 synthetic
holdings, one of them an unknown symbol). NSE's quote API answers an unknown symbol with a quick 404, but QuoteBatch
then re-warmed from that symbol's quote PAGE, which NSE never answers (ReadTimeout after 30 s), and the polite
client retried the timeout three more times.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from finresearch.adapters import http as http_mod
from finresearch.adapters import nse as nse_mod
from finresearch.adapters.http import is_transient
from finresearch.adapters.nse import NseNoQuote, Quote
from finresearch.portfolio.valuation import MAX_WARMS, QuoteBatch, fetch_prices

FIX = Path(__file__).parent / "fixtures" / "prices"
INFY = (FIX / "nse_quote_INFY_20260930_after_close.json").read_bytes()
UNKNOWN_404 = (FIX / "nse_quote_unknown_symbol_404_20261001.json").read_bytes()
RENAMED = (FIX / "nse_quote_renamed_ZOMATO_20261001.json").read_bytes()
UNKNOWN = "ZZNOSUCHSYM"


class FakeNse:
    """NSE as observed on 1-Oct-2026: quote pages set cookies, except an unknown symbol's page, which hangs; the
    quote API answers an unknown symbol with 404 and a renamed one with all-null sections."""

    def __init__(self) -> None:
        self.pages: list[str] = []
        self.api: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        sym = request.url.params.get("symbol", "")
        if request.url.path == "/get-quotes/equity":
            self.pages.append(sym)
            if sym == UNKNOWN:
                raise httpx.ReadTimeout("NSE never answered", request=request)
            return httpx.Response(200, html="<html>quote</html>", headers={"set-cookie": "nsit=abc; Path=/"})
        self.api.append(sym)
        if sym == UNKNOWN:
            return httpx.Response(404, content=UNKNOWN_404, headers={"content-type": "application/json"})
        if sym == "ZOMATO":
            return httpx.Response(200, content=RENAMED, headers={"content-type": "application/json"})
        return httpx.Response(200, content=INFY, headers={"content-type": "application/json"})


@pytest.fixture
def fake_nse(monkeypatch):
    """Every PoliteClient talks to FakeNse, without rate-limit waits or backoff sleeps (counted, not slept)."""
    fake = FakeNse()
    real = http_mod.PoliteClient

    class Offline(real):
        def __init__(self, **kw):
            async def nosleep(_s: float) -> None:
                return None

            kw.update(transport=httpx.MockTransport(fake), host_rates={}, default_rate=0, cache_dir=None,
                      sleep=nosleep)  # fmt: skip
            super().__init__(**kw)

    monkeypatch.setattr(http_mod, "PoliteClient", Offline)
    monkeypatch.setattr(nse_mod, "PoliteClient", Offline)
    return fake


async def test_an_unknown_symbol_never_triggers_a_warm_up_from_its_own_page(fake_nse):
    async with QuoteBatch() as batch:
        good = await batch.quote("INFY")
        with pytest.raises(LookupError):
            await batch.quote(UNKNOWN)
        again = await batch.quote("TCS")
    assert good.last_price is not None and again.last_price is not None
    assert UNKNOWN not in fake_nse.pages  # the page that hangs is never requested
    assert len(fake_nse.pages) <= MAX_WARMS  # and a 404 is "no quote", not a reason to re-warm
    assert fake_nse.api.count(UNKNOWN) == 1  # one fast 404, no retries


async def test_an_unknown_first_symbol_does_not_stall_the_batch(fake_nse):
    """The batch used to warm from the FIRST symbol's page: an unknown first symbol hung every quote behind it."""
    hs = [_H(1, UNKNOWN), _H(2, "INFY"), _H(3, "TCS"), _H(4, "SBIN")]
    async with QuoteBatch() as batch:
        out = await fetch_prices(hs, quote=batch.quote, scheme_rows=None)
    assert UNKNOWN not in fake_nse.pages and len(fake_nse.pages) <= MAX_WARMS
    assert out[1].price is None and "NseNoQuote" in out[1].error
    assert all(out[i].price is not None for i in (2, 3, 4))


async def test_a_renamed_symbols_null_quote_parses_as_no_price(fake_nse):
    async with QuoteBatch() as batch:
        q = await batch.quote("ZOMATO")
    assert isinstance(q, Quote) and q.last_price is None and q.close_price is None and q.as_of is None


async def test_a_hung_quote_is_cut_at_the_quote_timeout():
    class Batch(QuoteBatch):
        async def _quote(self, symbol: str, exchange: str):
            await asyncio.sleep(3600 if symbol == "SLOW" else 0)
            return Quote(symbol=symbol, last_price=1)

    t = time.perf_counter()
    async with Batch(quote_timeout=0.2) as batch:
        out = await fetch_prices([_H(1, "SLOW"), _H(2, "FAST")], quote=batch.quote, scheme_rows=None)
    assert time.perf_counter() - t < 1.5
    assert out[1].price is None and "TimeoutError" in out[1].error and out[2].price == 1


async def test_the_whole_fetch_is_bounded_by_its_budget():
    """A source that never answers (NAVAll download, a quote) cannot hold the page: unfinished rows say why and fall
    back to the last statement price."""

    async def quote(sym: str, exch: str = "NSE"):
        await asyncio.sleep(3600 if sym == "SLOW" else 0)
        return Quote(symbol=sym, last_price=10)

    async def navall() -> list:
        await asyncio.sleep(3600)
        return []

    stmt = _H(3, "SLOW2", meta={"statement_price": {"price": "99.5", "day": "2026-09-30", "source": "Groww"}})
    fund = _H(4, None, asset_type="mf", scheme_code="900",
              meta={"statement_nav": {"nav": "41.2", "day": "2026-09-29", "source": "CAS"}})  # fmt: skip

    async def quote2(sym: str, exch: str = "NSE"):
        return await quote("SLOW" if sym == "SLOW2" else sym, exch)

    t = time.perf_counter()
    out = await fetch_prices([_H(1, "SLOW"), _H(2, "FAST"), stmt, fund], quote=quote2, scheme_rows=navall,
                             budget_s=0.3)  # fmt: skip
    assert time.perf_counter() - t < 1.5
    assert out[2].price == 10
    assert out[1].price is None and "no price within 0.3 s" in out[1].error
    assert str(out[3].price) == "99.5" and "no price within" in out[3].error  # the statement close, flagged
    assert str(out[4].price) == "41.2" and "no price within" in out[4].error


def test_a_404_no_quote_is_not_transient_but_a_refusal_is():
    """`is_transient` matched "refused" in "NSE quote refused for X: HTTP 404", so an unknown symbol read as "NSE
    unreachable, retry" (negative-cached for seconds and re-fetched) instead of the exchange's "no data"."""
    msg = f"NSE has no quote for {UNKNOWN} (HTTP 404: unknown, delisted or renamed symbol)"
    assert not is_transient(NseNoQuote(msg)) and isinstance(NseNoQuote(msg), LookupError)
    assert is_transient(nse_mod.NseError("NSE quote refused for INFY: HTTP 403"))


async def test_nse_client_raises_no_quote_on_404(fake_nse):
    async with nse_mod.NseClient() as nse:
        with pytest.raises(NseNoQuote) as ei:
            await nse.quote(UNKNOWN, warm=False)
    assert not is_transient(ei.value)


class _H:
    def __init__(self, id: int, sym: str | None, *, asset_type: str = "stock", scheme_code: str | None = None,
                 meta: dict | None = None) -> None:  # fmt: skip
        self.id, self.asset_type, self.name = id, asset_type, f"Example {id} Ltd"
        self.nse_symbol, self.bse_code, self.isin, self.scheme_code = sym, None, None, scheme_code
        self.meta = meta or {}


def test_fixtures_are_the_recorded_bodies():
    assert json.loads(UNKNOWN_404) == {"error": "Unexpected end of JSON input"}
    assert json.loads(RENAMED)["equityResponse"][0]["metaData"] is None


def test_the_valuation_after_the_stream_does_not_refetch_a_failed_quote(env):
    """The page streams prices, then reads the full valuation at once: a quote that just failed must not be fetched a
    second time (a slow failure used to cost the first load twice)."""
    from datetime import date

    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                       "portfolio_snapshot, portfolio_setting"))  # fmt: skip
    asked: list[str] = []

    async def quote(sym: str) -> Quote:
        asked.append(sym)
        if sym == UNKNOWN:
            raise NseNoQuote(f"NSE has no quote for {sym} (HTTP 404)")
        return Quote(symbol=sym, last_price=100)

    async def navall() -> list:
        return []

    app = create_app(nav_all=navall)
    app.state.markets = MarketSources(quote=quote, today=lambda: date(2026, 9, 30))
    origin = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
    with TestClient(app) as c:
        for sym in ("INFY", UNKNOWN):
            assert c.post("/api/portfolio/transactions", headers=origin,
                          json={"asset_type": "stock", "name": f"Example {sym}", "nse_symbol": sym, "day": "2025-01-02",
                                "kind": "buy", "quantity": "10", "price": "90"}).status_code == 201  # fmt: skip
        with c.stream("GET", "/api/portfolio/prices/stream") as r:
            lines = [json.loads(x) for x in r.iter_lines() if x]
        assert lines[-1]["type"] == "done" and sorted(asked) == sorted(["INFY", UNKNOWN])
        full = c.get("/api/portfolio").json()
        assert sorted(asked) == sorted(["INFY", UNKNOWN])  # nothing fetched again
        bad = next(h for h in full["holdings"] if h["nse_symbol"] == UNKNOWN)
        assert bad["price"] is None and "NseNoQuote" in (bad["price_error"] or "")

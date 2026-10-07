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


async def test_a_renamed_nse_symbol_is_priced_through_its_isin():
    """Audit follow-up: a holding under its old NSE symbol (a rename like ZOMATO -> ETERNAL keeps the ISIN) showed "no
    price". NSE's 404 for the old symbol now looks the ISIN up in the NSE/BSE listings and quotes the new symbol."""
    from finresearch.adapters.bse_equity import Listing, Listings
    from finresearch.adapters.nse import NseNoQuote

    asked: list[str] = []
    loads = 0

    async def quote(sym: str, exch: str = "NSE") -> Quote:
        asked.append(sym)
        if sym in ("OLDCO", "GONECO", "SAMECO"):
            raise NseNoQuote(f"NSE has no quote for {sym} (HTTP 404)")
        return Quote(symbol=sym, last_price=D("212.40"))

    async def listings() -> Listings:
        nonlocal loads
        loads += 1
        return Listings(rows=[Listing(key="NEWCO", symbol="NEWCO", name="Example Ltd", isin="INE000X01011",
                                      exchange="NSE", exchanges=["NSE"], nse_symbol="NEWCO"),
                              Listing(key="SAMECO", symbol="SAMECO", name="Same Ltd", isin="INE000X01029",
                                      exchange="NSE", exchanges=["NSE"], nse_symbol="SAMECO")])  # fmt: skip

    hs = [H(1, "stock", "Example Ltd", nse_symbol="OLDCO", isin="ine000x01011"),
          H(2, "stock", "Example Ltd (2nd account)", nse_symbol="OLDCO", isin="INE000X01011"),
          H(3, "stock", "No ISIN", nse_symbol="GONECO"),
          H(4, "stock", "Same symbol", nse_symbol="SAMECO", isin="INE000X01029"),
          H(5, "stock", "Unaffected", nse_symbol="FINECO", isin="INE000X01037")]  # fmt: skip
    out = await fetch_prices(hs, quote=quote, scheme_rows=None, listings=listings)
    for hid in (1, 2):
        assert out[hid].price == D("212.40") and out[hid].error is None
        assert "OLDCO is now NEWCO" in out[hid].note and "INE000X01011" in out[hid].note
    assert out[3].price is None and out[3].note is None and "NseNoQuote" in out[3].error  # no ISIN: no guess
    assert out[4].price is None and out[4].note is None  # the listing has the same symbol: no second quote
    assert out[5].price == D("212.40") and out[5].note is None
    assert sorted(asked) == ["FINECO", "GONECO", "NEWCO", "OLDCO", "SAMECO"]  # NEWCO once for both holdings
    assert loads == 1  # the listings are loaded once (for every holding with an ISIN)


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


async def test_a_bse_only_isin_is_priced_on_bse_even_with_an_nse_looking_symbol():
    """#200: a broker statement stored "NSE$" as the NSE symbol of a BSE-only company. NSE's quote API still answered
    for "NSE$" (a last trade with no official close), so the holding showed a price that was not the company's BSE
    close. The ISIN listing says BSE only: quote BSE by its scrip code and never NSE."""
    from datetime import UTC, datetime

    from finresearch.adapters.bse_equity import Listing, Listings

    asked: list[tuple[str, str]] = []

    async def quote(sym: str, exch: str = "NSE") -> Quote:
        asked.append((sym, exch))
        at = datetime(2026, 10, 1, 10, 30, tzinfo=UTC)  # 16:00 IST: the session is over
        if exch == "NSE":
            return Quote(symbol=sym, last_price=D("1741.30"), as_of=at)  # no official close yet
        return Quote(symbol=sym, last_price=D("1723.50"), close_price=D("1723.50"), as_of=at)

    async def listings() -> Listings:
        return Listings(rows=[Listing(key="BSE:599901", symbol="EXAMPLE", name="Example Exchange Ltd",
                                      isin="INE000X01045", exchange="BSE", exchanges=["BSE"], bse_code="599901")])  # fmt: skip

    hs = [H(1, "stock", "Example Exchange Ltd", nse_symbol="NSE$", isin="INE000X01045")]
    out = await fetch_prices(hs, quote=quote, scheme_rows=None, listings=listings)
    assert out[1].price == D("1723.50") and out[1].source == "BSE quote: close (official)"
    assert asked == [("599901", "BSE")]
    # an ETF (INF... ISIN) is never in NSE's equity list: its absence is no "BSE only", NSE still quotes it
    asked.clear()

    async def etf_listings() -> Listings:
        return Listings(rows=[Listing(key="BSE:590103", symbol="EXBEES", name="Example Nifty ETF", isin="INF000X01011",
                                      exchange="BSE", exchanges=["BSE"], bse_code="590103")])  # fmt: skip

    etf = [H(2, "stock", "Example Nifty ETF", nse_symbol="EXBEES", isin="INF000X01011")]
    await fetch_prices(etf, quote=quote, scheme_rows=None, listings=etf_listings)
    assert asked[0] == ("EXBEES", "NSE")


async def test_after_the_session_bse_official_close_beats_an_nse_last_trade():
    """#200: after 15:30 IST with NSE's official close not yet published, a stock listed on both exchanges takes BSE's
    published official close (when BSE has one) instead of NSE's last trade; before the close NSE's live price stays."""
    from datetime import UTC, datetime

    def make(at: datetime, bse_close: D | None):
        async def quote(sym: str, exch: str = "NSE") -> Quote:
            if exch == "NSE":
                return Quote(symbol=sym, last_price=D("501.00"), as_of=at)
            return Quote(symbol=sym, last_price=D("500.00"), close_price=bse_close, as_of=at)

        return quote

    hs = [H(1, "stock", "Example Ltd", nse_symbol="EXAMPLE", bse_code="599902")]
    from finresearch.fincalc.dates import to_ist

    # price_from_quote compares with the real clock: an earlier IST day is "over", so use today's IST date
    today = to_ist(datetime.now(UTC)).date()
    after = datetime(today.year, today.month, today.day, 10, 30, tzinfo=UTC)  # 16:00 IST
    out = await fetch_prices(hs, quote=make(after, D("499.80")), scheme_rows=None)
    assert out[1].price == D("499.80") and out[1].source == "BSE quote: close (official)"
    # BSE has no official close either: keep NSE's last trade and its "close not yet published" label
    out = await fetch_prices(hs, quote=make(after, None), scheme_rows=None)
    assert out[1].price == D("501.00") and out[1].source.startswith("NSE quote: last traded")
    live = datetime(today.year, today.month, today.day, 6, 0, tzinfo=UTC)  # 11:30 IST, in session
    out = await fetch_prices(hs, quote=make(live, D("499.80")), scheme_rows=None)
    assert out[1].price == D("501.00") and out[1].source == "NSE quote: last traded"


def test_portfolio_gets_never_record_a_snapshot_the_post_does(client):
    """#247: GET /api/portfolio (and /api/portfolio/health, which calls it) upserted the day's portfolio_snapshot,
    the value history behind the drawdown and drift alerts, so a prefetch or a retry changed alert inputs. Only
    POST /api/portfolio/snapshot (and the monitor's daily pass) records it, and only once every price is cached."""
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioSnapshot

    c, f = client

    def snaps():
        with session_scope() as s:
            return [(x.day, x.value, x.complete) for x in s.query(PortfolioSnapshot)]

    early = c.post(
        "/api/portfolio/snapshot", headers=ORIGIN
    ).json()  # nothing cached yet: refused, no network
    assert early == {"recorded": False, "reason": "9 holding(s) have no current price yet"} and f.quotes == []
    full = c.get("/api/portfolio").json()  # live prices, every holding valued
    assert full["pending"] == 0 and full["complete"]
    c.get("/api/portfolio?prices=cached")
    c.get("/api/portfolio/health")
    assert snaps() == []
    r = c.post("/api/portfolio/snapshot", headers=ORIGIN).json()
    assert r == {"recorded": True, "day": "2026-09-30", "complete": True}
    assert snaps() == [(date(2026, 9, 30), D("8500.00"), True)]  # 8 stocks x 10 x 100 + 10 units x 50

"""Groww market data (#267): read-only allowlist, batching, market-hours caches, coalescing, rate buckets, the instruments
file, option chains and provenance. Every Groww response is synthetic, shaped after the samples in the official docs
(adapters.groww_market docstring); no network, no real account, no real token."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from finresearch.adapters import groww_budget, groww_market
from finresearch.adapters.groww_market import GrowwMarket
from finresearch.fincalc.dates import IST
from finresearch.portfolio.connectors import base
from finresearch.portfolio.connectors.base import ForbiddenRequest

TOKEN = "-".join(["fake", "access"]) + "z" * 20  # assembled at runtime: no key-like literal (gitleaks)
# a Friday in session, the same Friday after 16:00, and the Saturday after (no NSE holiday on these dates)
FRI_10 = datetime(2026, 10, 9, 10, 0, tzinfo=IST)
FRI_17 = datetime(2026, 10, 9, 17, 0, tzinfo=IST)
SAT = datetime(2026, 10, 10, 11, 0, tzinfo=IST)

CSV_HEAD = ("exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
            "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,"
            "freeze_quantity,is_reserved,buy_allowed,sell_allowed\n")  # fmt: skip


def csv_rows(n: int = 3) -> str:
    rows = [f"NSE,{1000 + i},EXSTK{i},NSE-EXSTK{i},Example Stock {i},EQ,CASH,EQ,INE00000{i:04d}A,,,,,1,0.05,,0,1,1"
            for i in range(n)]  # fmt: skip
    rows += ["BSE,500999,EXBSE,BSE-EXBSE,Example BSE Only,EQ,CASH,A,INE999Z01010,,,,,1,0.05,,0,1,1",
             "NSE,26000,NIFTY,NSE-NIFTY,Nifty 50,IDX,CASH,,,,,,,1,0.05,,0,0,0",
             "NSE,35001,NIFTY26OCT25000CE,NSE-NIFTY-27Oct26-25000-CE,NIFTY,CE,FNO,,,NIFTY,26000,2026-10-27,25000,75,"
             "0.05,,0,1,1"]  # fmt: skip
    return CSV_HEAD + "\n".join(rows) + "\n"


class FakeSession:
    def __init__(self, token: str | None = TOKEN) -> None:
        self._token, self.state = token, "connected" if token else "not_connected"

    def token(self) -> str | None:
        return self._token

    def forget(self) -> None:
        self._token, self.state = None, "refused"


class Groww:
    """MockTransport for api.groww.in and growwapi-assets.groww.in that records every request."""

    def __init__(self, *, ltp_status: int = 200, delay: float = 0.0, n_csv: int = 3) -> None:
        self.requests: list[httpx.Request] = []
        self.ltp_status, self.delay, self.csv = ltp_status, delay, csv_rows(n_csv)
        self.closes = {date(2026, 10, 7): 100.0, date(2026, 10, 8): 101.0, date(2026, 10, 9): 103.5}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        path = request.url.path
        if request.url.host == "growwapi-assets.groww.in" and path == "/instruments/instrument.csv":
            return httpx.Response(200, content=self.csv.encode())
        if path == "/v1/live-data/ltp":
            if self.ltp_status != 200:
                return httpx.Response(
                    self.ltp_status, json={"status": "FAILURE", "error": {"message": "down"}}
                )
            keys = request.url.params.get_list("exchange_symbols")
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {k: 200.25 for k in keys
                                                                               if k != "NSE_EXSTK2"}})  # fmt: skip
        if path == "/v1/historical/candles":
            lo = date.fromisoformat(request.url.params["start_time"][:10])
            hi = date.fromisoformat(request.url.params["end_time"][:10])
            rows = [[f"{d.isoformat()}T00:00:00", c - 1, c + 1, c - 2, c, 1000, None]
                    for d, c in sorted(self.closes.items()) if lo <= d <= hi]  # fmt: skip
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"candles": rows}})
        if re.fullmatch(r"/v1/option-chain/exchange/NSE/underlying/NIFTY", path):
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"underlying_ltp": 25641.7, "strikes": {
                "25600": {"CE": {"greeks": {"delta": 0.52, "iv": 12.5}, "trading_symbol": "X", "ltp": 120.5,
                                 "open_interest": 1000, "volume": 50},
                          "PE": {"greeks": {"delta": -0.48, "iv": 13.1}, "ltp": 80, "open_interest": 1500,
                                 "volume": 70}},
                "25700": {"CE": {"greeks": {"iv": 12.0}, "ltp": 70, "open_interest": 400, "volume": 10}}}}})  # fmt: skip
        return httpx.Response(404, json={"status": "FAILURE", "error": {"message": f"no route {path}"}})

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t.astimezone(UTC)


@pytest.fixture
def groww(monkeypatch):
    """Install the mock transport and a no-wait budget; returns (transport, make_client)."""

    class NoWait:
        def reserve(self, key, interval):
            return 0.0

        def hold_off(self, key, seconds):
            pass

    monkeypatch.setattr(groww_budget.BUDGET, "slots", NoWait())

    def install(**kw):
        g = Groww(**kw)
        monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(g))

        def client(t: datetime = FRI_10, token: str | None = TOKEN) -> tuple[GrowwMarket, Clock]:
            clock = Clock(t)
            return GrowwMarket(session=FakeSession(token), clock=clock, holidays=set), clock

        return g, client

    return install


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ read-only: only allow-listed GETs, two hosts
def test_only_allowlisted_get_paths_and_hosts_are_reachable(groww):
    g, client = groww()
    m, _ = client()

    async def go():
        await m.quotes([("EXSTK0", "NSE"), ("500999", "BSE")])
        await m.option_chain("NIFTY", date(2026, 10, 27))
        await m.history_closes("NSE", "EXSTK1", date(2026, 9, 1), date(2026, 10, 8))
        api = m._api(TOKEN)
        for path in ("/order/create", "/order/cancel", "/order/list", "/holdings/user", "/margins/detail/orders",
                     "/api/apex/v1/socket/token/create/", "/order-advance/create", "/live-data/quote"):  # fmt: skip
            with pytest.raises(ForbiddenRequest):
                await api.get(path)
        with pytest.raises(ForbiddenRequest):
            await api.auth_post("/live-data/ltp", json={})

    run(go())
    assert g.requests
    assert {r.method for r in g.requests} == {"GET"}
    assert {r.url.host for r in g.requests} <= {"api.groww.in", "growwapi-assets.groww.in"}
    allowed = [re.compile("/v1" + p) for p in groww_market.MARKET_PATHS] + [
        re.compile(r"/instruments/instrument\.csv")
    ]
    assert all(any(p.fullmatch(r.url.path) for p in allowed) for r in g.requests)
    # the instruments file is fetched without the Groww token
    assert all(
        "authorization" not in r.headers for r in g.requests if r.url.host == "growwapi-assets.groww.in"
    )


# ------------------------------------------------------------------ batching, market-hours TTL, closed days
def test_ltp_is_batched_fifty_instruments_a_call(groww):
    g, client = groww(n_csv=120)
    m, _ = client()
    got = run(m.ltp([f"NSE_EXSTK{i}" for i in range(120)]))
    sizes = [len(r.url.params.get_list("exchange_symbols")) for r in g.calls("/v1/live-data/ltp")]
    assert sizes == [50, 50, 20]
    assert len(got) == 119 and "NSE_EXSTK2" not in got  # Groww did not price it: absent, never 0


def test_cache_ttl_across_the_open_and_close(groww):
    g, client = groww()
    m, clock = client(FRI_10)
    items = [("EXSTK0", "NSE"), ("EXSTK1", "NSE")]
    q = run(m.quotes(items))
    assert len(g.calls("/v1/live-data/ltp")) == 1 and len(g.calls("/v1/historical/candles")) == 2
    assert q[items[0]].last_price == 200.25 and str(q[items[0]].previous_close) == "101.0"
    assert q[items[0]].close_price is None  # in session: no close from a still-forming candle
    clock.t = FRI_10 + timedelta(seconds=10)
    run(m.quotes(items))
    assert len(g.calls("/v1/live-data/ltp")) == 1  # within LTP_TTL_S: memory
    clock.t = FRI_10 + timedelta(seconds=16)
    run(m.quotes(items))
    assert len(g.calls("/v1/live-data/ltp")) == 2  # expired: one new batch for both
    assert len(g.calls("/v1/historical/candles")) == 2  # previous closes stay cached until 16:00
    clock.t = FRI_17
    q = run(m.quotes(items))
    assert len(g.calls("/v1/live-data/ltp")) == 2  # after the session: no LTP calls at all
    assert len(g.calls("/v1/historical/candles")) == 4  # one read per stock for today's close
    assert str(q[items[0]].close_price) == "103.5" and q[items[0]].as_of.date() == date(2026, 10, 9)
    assert q[items[0]].provenance == "Groww daily close"
    n = len(g.requests)
    clock.t = SAT
    run(m.quotes(items))
    fresh, _ = client(SAT)  # a restarted process on the weekend: the closes come from the disk cache
    q2 = run(fresh.quotes(items))
    assert (
        len(g.requests) == n + 1
    )  # only the restarted process's instruments read (once a day): no Groww data call
    assert [r.url.host for r in g.requests[n:]] == ["growwapi-assets.groww.in"]
    assert str(q2[items[0]].close_price) == "103.5"


def test_close_not_yet_at_groww_is_retried_not_cached_until_monday(groww):
    g, client = groww()
    g.closes.pop(date(2026, 10, 9))
    m, clock = client(FRI_17)
    q = run(m.quotes([("EXSTK0", "NSE")]))
    assert q[("EXSTK0", "NSE")].as_of.date() == date(
        2026, 10, 8
    )  # the latest close Groww has, with its own date
    g.closes[date(2026, 10, 9)] = 103.5
    clock.t = FRI_17 + timedelta(minutes=11)
    q = run(m.quotes([("EXSTK0", "NSE")]))
    assert str(q[("EXSTK0", "NSE")].close_price) == "103.5"


def test_concurrent_identical_requests_are_coalesced(groww):
    g, client = groww(delay=0.05)
    m, _ = client()
    items = [("EXSTK0", "NSE"), ("EXSTK1", "NSE")]

    async def go():
        return await asyncio.gather(m.quotes(items), m.quotes(items), m.quotes(list(reversed(items))))

    a, b, c = run(go())
    assert a.keys() == b.keys() == c.keys() and len(a) == 2
    assert len(g.calls("/v1/live-data/ltp")) == 1
    assert len(g.calls("/v1/historical/candles")) == 2
    assert len(g.calls("/instruments/instrument.csv")) == 1


def test_instruments_file_is_downloaded_once_a_day(groww):
    g, client = groww()
    m, clock = client()
    run(m.instruments())
    other, _ = client()  # another process the same day reads the cached file
    run(other.instruments())
    assert len(g.calls("/instruments/instrument.csv")) == 1
    clock.t = FRI_10 + timedelta(days=3)
    run(m.instruments())
    assert len(g.calls("/instruments/instrument.csv")) == 2


def test_failures_fall_back_and_unknown_instruments_are_absent(groww):
    g, client = groww(ltp_status=500)
    m, _ = client()
    q = run(m.quotes([("EXSTK0", "NSE"), ("NOTLISTED", "NSE")]))
    assert q == {}  # Groww failed: nothing, so the caller asks NSE/BSE (never a price of 0)
    run(m.quotes([("EXSTK0", "NSE")]))
    assert len(g.calls("/v1/live-data/ltp")) == 1  # a failure is not repeated for FAIL_TTL_S
    m2, _ = client(token=None)
    assert run(m2.quotes([("EXSTK0", "NSE")])) == {} and run(m2.history_closes("NSE", "EXSTK0", FRI_10.date(),
                                                                                FRI_10.date())) is None  # fmt: skip


def test_option_chain_from_groww(groww):
    g, client = groww()
    m, _ = client()
    ch = run(m.option_chain("nifty", date(2026, 10, 27)))
    assert [str(r.strike) for r in ch.rows] == ["25600", "25700"]
    atm = ch.atm()
    assert str(atm.strike) == "25600" and str(atm.call.iv) == "12.5" and str(atm.put.oi) == "1500"
    assert atm.call.change_in_oi is None and atm.call.bid is None  # not in Groww's chain: unknown, not 0
    assert ch.rows[1].put is None
    run(m.option_chain("NIFTY", date(2026, 10, 27)))
    assert len(g.calls("/v1/option-chain/exchange/NSE/underlying/NIFTY")) == 1
    assert (
        g.calls("/v1/option-chain/exchange/NSE/underlying/NIFTY")[0].url.params["expiry_date"] == "2026-10-27"
    )


def test_provenance_label_on_the_price(groww):
    from finresearch.portfolio.valuation import price_from_quote

    _g, client = groww()
    m, _ = client()
    q = run(m.quotes([("EXSTK0", "NSE")]))[("EXSTK0", "NSE")]
    p = price_from_quote(q, "NSE")
    assert p.source.startswith("Groww LTP") and str(p.price) == "200.25"


# ------------------------------------------------------------------ valuation: Groww first, fall back, provenance
class _H:
    def __init__(self, i: int, asset_type: str, sym: str | None = None, scheme: str | None = None) -> None:
        self.id, self.asset_type, self.name, self.nse_symbol, self.bse_code = (
            i,
            asset_type,
            f"H{i}",
            sym,
            None,
        )
        self.isin, self.scheme_code, self.meta = None, scheme, {}


def test_valuation_prefers_groww_and_falls_back_per_stock(groww):
    from decimal import Decimal

    from finresearch.adapters.amfi import SchemeNav
    from finresearch.adapters.groww_market import Prefetch
    from finresearch.adapters.nse import Quote
    from finresearch.portfolio import valuation
    from finresearch.portfolio.valuation import fetch_prices

    g, client = groww()
    m, _ = client()
    asked: list[str] = []

    async def nse(sym: str, exch: str = "NSE") -> Quote:
        asked.append(sym)
        if sym == "NOTLISTED":
            raise LookupError("no quote")
        return Quote(symbol=sym, last_price=Decimal("99.5"), industry="Example Industry")

    async def navs() -> list:
        return [
            SchemeNav(
                "900", "Fund", "Direct", "Growth", None, None, Decimal(50), date(2026, 10, 8), "Equity", "X"
            )
        ]

    hs = [
        _H(1, "stock", "EXSTK0"),
        _H(2, "stock", "EXSTK2"),
        _H(3, "stock", "NOTLISTED"),
        _H(4, "mf", scheme="900"),
    ]
    valuation._META.clear()
    out = run(fetch_prices(hs, quote=nse, scheme_rows=navs, prefetch=Prefetch(m)))
    assert out[1].source == "Groww LTP: last traded" and str(out[1].price) == "200.25"
    assert out[1].industry == "Example Industry"  # one exchange quote for the sector label, then remembered
    assert out[2].source.startswith("NSE quote") and str(out[2].price) == "99.5"  # Groww had no price: NSE
    assert out[3].price is None and out[3].error  # neither source: unknown, never 0
    assert out[4].source == "AMFI NAVAll"  # funds never go to Groww
    assert sorted(asked) == ["EXSTK0", "EXSTK2", "NOTLISTED"]
    asked.clear()
    run(fetch_prices(hs[:1], quote=nse, scheme_rows=None, prefetch=Prefetch(m)))
    assert asked == []  # the industry is remembered: a Groww-priced refresh asks NSE nothing
    # Groww down: every stock takes the exchange path, nothing breaks
    g.ltp_status = 503
    m2, _ = client()
    out = run(fetch_prices(hs[:1], quote=nse, scheme_rows=None, prefetch=Prefetch(m2)))
    assert out[1].source.startswith("NSE quote")


# ------------------------------------------------------------------ price history: Groww for the daily top-up only
def test_history_top_up_from_groww_backfill_and_split_ranges_from_the_exchange(groww):
    from types import SimpleNamespace

    from finresearch.portfolio.history import Fetcher, Instrument, PriceStore

    g, client = groww()
    m, _ = client(FRI_17)
    asked: list[tuple[date, date]] = []

    class Eq:
        answers_full_range = True

        async def history(self, sym, lo, hi):
            asked.append((lo, hi))
            return [SimpleNamespace(day=lo, close=50.0)]

    class Cm:
        async def __aenter__(self):
            return Eq()

        async def __aexit__(self, *a):
            return None

    sources = SimpleNamespace(open_equity=lambda exchange="NSE": Cm(), today=lambda: date(2026, 10, 9))
    inst = Instrument("NSE:EXSTK0", "nse", "EXSTK0")

    async def go(tmp):
        f = Fetcher(sources, groww=m)
        store = PriceStore(tmp)
        got = await store.get(inst.key, date(2026, 10, 1), date(2026, 10, 9), lambda a, b: f(inst, a, b),
                              date(2026, 10, 9))  # fmt: skip
        assert got == {date(2026, 10, 7): 100.0, date(2026, 10, 8): 101.0, date(2026, 10, 9): 103.5}
        assert f.provenance[inst.key] == "Groww" and asked == []
        # a years-long backfill stays on the exchange's raw closes
        await f(inst, date(2024, 1, 1), date(2024, 6, 30))
        assert asked == [(date(2024, 1, 1), date(2024, 6, 30))] and f.provenance[inst.key] == "NSE"
        # a recorded split inside the range: raw closes needed, so the exchange
        f.split_days[inst.key] = {date(2026, 10, 5)}
        await f(inst, date(2026, 10, 1), date(2026, 10, 9))
        assert len(asked) == 2
        # Groww failing: the exchange answers
        f2 = Fetcher(sources, groww=m)
        g.closes = {}
        m._windows.clear()
        assert (await f2(inst, date(2026, 9, 20), date(2026, 9, 30)))[0] == {}  # Groww answered "no candles"
        broken, _ = client(FRI_17, token=None)
        f3 = Fetcher(sources, groww=broken)
        await f3(inst, date(2026, 10, 1), date(2026, 10, 9))
        assert f3.provenance[inst.key] == "NSE"

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        run(go(Path(d)))
    assert len(g.calls("/v1/historical/candles")) == 2


def test_fno_chain_groww_first_then_nse(groww, monkeypatch):
    from finresearch.adapters.groww_fno import GrowwFirstFno
    from finresearch.adapters.nse_fno import NseFno, OptionChain

    _g, client = groww()
    m, _ = client()
    nse_asked = []

    async def nse_chain(self, symbol, expiry):
        nse_asked.append(symbol)
        return OptionChain(symbol=symbol, expiry=expiry, underlying=None, as_of=None, rows=[])

    monkeypatch.setattr(NseFno, "option_chain", nse_chain)

    async def go():
        f = GrowwFirstFno(client=object(), market=m)
        a = await f.option_chain("NIFTY", date(2026, 10, 27))
        b = await f.option_chain("RELIANCE", date(2026, 10, 27))  # Groww has none (404): NSE answers
        off, _ = client(token=None)
        c = await GrowwFirstFno(client=object(), market=off).option_chain("NIFTY", date(2026, 10, 27))
        return a, b, c

    a, b, c = run(go())
    assert a.source == "Groww" and len(a.rows) == 2
    assert b.source == "NSE" and c.source == "NSE" and nse_asked == ["RELIANCE", "NIFTY"]


def test_index_and_stock_live_value_from_groww_on_the_intraday_chart(env, groww, monkeypatch):
    from decimal import Decimal

    from fastapi.testclient import TestClient

    from finresearch.adapters.nse_intraday import IntradaySeries, Tick
    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import IntradaySeriesRow

    with session_scope() as s:
        s.query(IntradaySeriesRow).delete()
    monkeypatch.setattr("finresearch.api.live._holidays", lambda: {})
    g, client = groww()
    t = datetime(2026, 10, 9, 9, 40, tzinfo=IST)
    m, _ = client(t)
    monkeypatch.setattr(groww_market, "MARKET", m)

    async def fetch(kind, sym):
        ticks = [
            Tick(datetime(2026, 10, 9, 9, 15 + i, 59, tzinfo=IST), Decimal(p))
            for i, p in enumerate([190, 195])
        ]
        return IntradaySeries(sym, kind, Decimal(180), ticks, "src")

    app = create_app(clock=lambda: t)
    app.state.intraday_fetch = fetch
    with TestClient(app) as c:
        idx = c.get("/api/indices/NIFTY%2050/intraday").json()
        stk = c.get("/api/stocks/EXSTK0/intraday").json()
        unknown = c.get("/api/stocks/NOTLISTED/intraday").json()
    assert idx["last"] == 200.25 and idx["last_source"] == "Groww" and idx["change"] == 20.25
    assert any("Groww" in n for n in idx["notes"])
    assert stk["last"] == 200.25 and stk["last_source"] == "Groww"
    assert (
        unknown["last"] == 195 and unknown["last_source"] == "NSE"
    )  # not in Groww's file: the exchange's value
    assert len(g.calls("/v1/live-data/ltp")) == 2  # NIFTY and EXSTK0 (each cached for LTP_TTL_S)


# ------------------------------------------------------------------ rate buckets and daily counts
def test_rate_buckets_are_shared_per_category_and_counted(tmp_path):
    from finresearch.adapters.groww_budget import AUTH_DAILY_GUARD, Budget, BudgetExceeded, category, interval
    from finresearch.adapters.http import SharedSlots

    now = {"t": 1000.0}
    slots = SharedSlots(tmp_path / "slots", clock=lambda: now["t"])
    day = {"d": date(2026, 10, 9)}
    api, monitor = (
        Budget(slots, tmp_path / "counts", today=lambda: day["d"]) for _ in range(2)
    )  # two processes
    # Live Data: 10/s and 300/min -> 0.2 s apart; Non Trading: 20/s and 500/min -> 0.12 s; Auth: 5/s, 30/min -> 2 s
    assert (interval("live"), interval("non_trading"), interval("auth")) == (0.2, 0.12, 2.0)
    waits = [api.wait_s("live"), monitor.wait_s("live"), api.wait_s("live"), monitor.wait_s("non_trading")]
    assert [round(w, 3) for w in waits] == [0.0, 0.2, 0.4, 0.0]  # one bucket per category, shared by both
    slots_per_min = 0
    t0 = now["t"]
    while api.wait_s("live") + (now["t"] - t0) < 60:
        slots_per_min += 1
    assert slots_per_min <= 300 - 3  # never more than 300 a minute (3 already taken above)
    api.hold_off("live", 5)  # a 429: everyone backs off
    assert monitor.wait_s("live") >= 5
    assert {category("/live-data/ltp"), category("/historical/candles"), category("/option-chain/exchange/NSE/"
            "underlying/NIFTY")} == {"live"}  # fmt: skip
    assert category("/holdings/user") == "non_trading" and category("/token/api/access") == "auth"
    for _ in range(AUTH_DAILY_GUARD):
        api.wait_s("auth")
    with pytest.raises(BudgetExceeded):
        monitor.wait_s("auth")  # Groww allows 150 tokens a day: stop short, before sending
    usage = {r["category"]: r["calls"] for r in api.today()["categories"]}
    assert usage["auth"] == AUTH_DAILY_GUARD and usage["non_trading"] == 1 and usage["live"] > 300
    day["d"] = date(2026, 10, 10)
    assert api.wait_s("auth") >= 0  # a new IST day: a new count


def test_groww_daily_close_is_not_labelled_official(groww):
    from finresearch.portfolio.valuation import price_from_quote

    _g, client = groww()
    m, _ = client(FRI_17)
    p = price_from_quote(run(m.quotes([("EXSTK0", "NSE")]))[("EXSTK0", "NSE")], "NSE")
    assert str(p.price) == "103.5" and p.source.startswith("Groww daily close")
    assert "official close" not in p.source.replace("not verified as the exchange's official close", "")


def test_the_daily_login_runs_from_the_monitor_tick_once_never_from_a_read(monkeypatch):
    calls = []

    async def login():
        calls.append(1)

    monkeypatch.setattr(groww_market, "_read_token", lambda now: (None, None, "expired", True))
    monkeypatch.setattr(groww_market, "_login", login)
    # reading the session (what every GET does) never logs in
    s = groww_market.Session(clock=lambda: FRI_10.astimezone(UTC))
    assert s.token() is None and calls == []
    assert run(groww_market.session_step(SAT)) == {}  # a closed day: no login
    assert run(groww_market.session_step(FRI_10)) == {"groww_market_login": 1}
    assert run(groww_market.session_step(FRI_10 + timedelta(minutes=5))) == {}  # once a day, across processes
    assert calls == [1]
    monkeypatch.setattr(groww_market, "_read_token", lambda now: (None, None, "off", False))
    assert run(groww_market.session_step(FRI_10 + timedelta(days=3))) == {}  # connection or auto-sync off

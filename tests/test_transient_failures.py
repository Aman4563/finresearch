"""A network or exchange failure (DNS, timeout, 403, a block page instead of JSON) is never cached as data or as
"no data": the next request (or the UI's Retry) asks the exchange again, and only genuine answers keep the full TTL.
Offline: a fake httpx transport plays BSE, first unreachable, then answering from recorded payloads."""

from __future__ import annotations

import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from test_bse_equity import TODAY, XBRL, load, quote

from finresearch.adapters.bse import BseClient, BseError
from finresearch.adapters.bse_equity import BseEquity
from finresearch.adapters.http import PoliteClient, is_transient
from finresearch.adapters.nse import NseError
from finresearch.api import markets
from finresearch.api.markets import NEGATIVE_TTL_S, MarketSources, TtlCache

DNS = "[Errno 8] nodename nor servname provided, or not known"


# --------------------------------------------------------------------------- classification
@pytest.mark.parametrize(("exc", "transient"), [
    (httpx.ConnectError(DNS), True),
    (httpx.ReadTimeout("timed out"), True),
    (socket.gaierror(8, "nodename nor servname provided"), True),
    (NseError("NSE HTTP 403 for /api/quote-equity"), True),
    (NseError("NSE refused https://www.nseindia.com/api/x after re-warm: HTTP 401"), True),
    (BseError("BSE returned a non-JSON page for https://api.bseindia.com/x"), True),
    (BseError("BSE HTTP 503 for https://api.bseindia.com/x"), True),
    (BseError("HTTP 404 for https://www.bseindia.com/XBRLFILES/x.xml"), False),  # the file is not there: genuine
    (ValueError("bad XBRL"), False),
    (LookupError("NSE has no quote for XYZ"), False),
])  # fmt: skip
def test_is_transient(exc, transient):
    assert is_transient(exc) is transient


def test_is_transient_follows_the_cause_chain():
    try:
        try:
            raise httpx.ConnectError(DNS)
        except httpx.ConnectError as e:
            raise LookupError("BSE has no quote for scrip 526433") from e
    except LookupError as wrapped:
        assert is_transient(wrapped)


# --------------------------------------------------------------------------- the route cache
async def test_cache_keeps_a_degraded_answer_briefly_and_retry_skips_it(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(markets.time, "time", lambda: clock["t"])
    cache, calls = TtlCache(), {"n": 0}

    async def make():
        calls["n"] += 1
        return (
            {"quarters": [], "unreachable": ["integrated filing index"]}
            if calls["n"] == 1
            else {"quarters": [1]}
        )

    assert (await cache.get("k", 12 * 3600, make))["unreachable"]
    assert (await cache.get("k", 12 * 3600, make))["unreachable"] and calls[
        "n"
    ] == 1  # polling does not hammer
    assert await cache.get("k", 12 * 3600, make, retry=True) == {"quarters": [1]}  # Retry asks again at once
    clock["t"] += 6 * 3600
    assert (
        await cache.get("k", 12 * 3600, make, retry=True) == {"quarters": [1]} and calls["n"] == 2
    )  # data stays

    other = TtlCache()
    first = {"n": 0}

    async def flaky():
        first["n"] += 1
        return (
            {"x": [], "unreachable": ["announcements"]}
            if first["n"] == 1
            else {"x": ["a"], "unreachable": []}
        )

    await other.get("o", 600, flaky)
    clock["t"] += NEGATIVE_TTL_S + 1  # the short negative TTL has passed: the next request fetches again
    assert (await other.get("o", 600, flaky))["x"] == ["a"]


async def test_cache_never_stores_an_exception():
    cache, calls = TtlCache(), {"n": 0}

    async def make():
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError(DNS)
        return {"ok": True}

    with pytest.raises(httpx.ConnectError):
        await cache.get("k", 3600, make)
    assert await cache.get("k", 3600, make) == {"ok": True}


async def test_a_genuine_empty_answer_keeps_the_full_ttl(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(markets.time, "time", lambda: clock["t"])
    cache, calls = TtlCache(), {"n": 0}

    async def make():
        calls["n"] += 1
        return {"quarters": [], "unreachable": []}  # the exchange answered: nothing is filed

    await cache.get("k", 3600, make)
    clock["t"] += 3000
    await cache.get("k", 3600, make, retry=True)
    assert calls["n"] == 1


# --------------------------------------------------------------------------- end to end through a fake transport
class FakeBseNet:
    """httpx transport playing BSE's API and XBRL files; `down` makes every request fail like a DNS error."""

    def __init__(self):
        self.down = True
        self.requests: list[str] = []
        self.xbrl = {u: p.read_bytes() for u, p in XBRL.items()}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(str(req.url))
        if self.down:
            raise httpx.ConnectError(DNS, request=req)
        if req.url.path.endswith("/Integratedfinancedata/w"):
            return httpx.Response(200, json=load("integrated_financials_526433.json"))
        url = str(req.url.copy_with(query=None))
        if url in self.xbrl:
            return httpx.Response(200, content=self.xbrl[url], headers={"content-type": "text/xml"})
        return httpx.Response(404, text="<html>not found</html>")


@pytest.fixture
def flaky_app(env, tmp_path):
    from finresearch.api import create_app

    net = FakeBseNet()

    async def no_sleep(_):
        return None

    http = PoliteClient(transport=httpx.MockTransport(net), cache_dir=tmp_path, sleep=no_sleep, max_retries=0)

    async def bse_quote(code):
        return quote(code)

    app = create_app()
    app.state.markets = MarketSources(bse_equity=lambda: BseEquity(BseClient(http)), bse_quote=bse_quote,
                                      today=lambda: TODAY)  # fmt: skip
    with TestClient(app) as c:
        yield c, net


def test_results_route_retries_after_a_dns_failure(flaky_app):
    c, net = flaky_app
    first = c.get("/api/stocks/BSE:526433/results", params={"quarters": 4}).json()
    assert first["quarters"] == [] and first["unreachable"] == ["integrated filing index"]
    assert any("ConnectError" in e for e in first["errors"])
    net.down = False  # BSE is reachable again
    second = c.get("/api/stocks/BSE:526433/results", params={"quarters": 4, "retry": True}).json()
    assert second["unreachable"] == [] and any(q["period_end"] == "2026-06-30" for q in second["quarters"])
    # the good answer is cached: a plain request serves it without asking BSE again
    n = len(net.requests)
    assert c.get("/api/stocks/BSE:526433/results", params={"quarters": 4}).json() == second
    assert len(net.requests) == n


def test_results_route_without_retry_refetches_once_the_short_negative_ttl_passes(flaky_app, monkeypatch):
    c, net = flaky_app
    clock = {"t": 5000.0}
    monkeypatch.setattr(markets.time, "time", lambda: clock["t"])
    assert c.get("/api/stocks/BSE:526433/results").json()["unreachable"]
    net.down = False
    assert c.get("/api/stocks/BSE:526433/results").json()[
        "unreachable"
    ]  # within NEGATIVE_TTL_S: cached, no hammering
    clock["t"] += NEGATIVE_TTL_S + 1
    assert c.get("/api/stocks/BSE:526433/results").json()["quarters"]


# --------------------------------------------------------------------------- the adapters' disk cache
async def test_bse_disk_cache_never_keeps_a_block_page(tmp_path):
    answers = [
        httpx.Response(200, text="<html>Access Denied</html>"),
        httpx.Response(200, json={"Table": []}),
    ]
    seen: list = []

    def handler(req):
        seen.append(req)
        return answers[min(len(seen) - 1, 1)]

    async def no_sleep(_):
        return None

    http = PoliteClient(transport=httpx.MockTransport(handler), cache_dir=tmp_path, sleep=no_sleep)
    async with BseClient(http) as bse:
        with pytest.raises(BseError, match="non-JSON"):
            await bse.get_json("/ListofScripData/w", {"segment": "Equity"}, cache_ttl=86400)
        assert (await bse.get_json("/ListofScripData/w", {"segment": "Equity"}, cache_ttl=86400))[0] == {
            "Table": []
        }
        assert (await bse.get_json("/ListofScripData/w", {"segment": "Equity"}, cache_ttl=86400))[0] == {
            "Table": []
        }
    await http.aclose()
    assert len(seen) == 2  # the block page was not cached; the JSON was


# --------------------------------------------------------------------------- signals
def test_stock_signal_inputs_with_an_unreachable_part_are_not_kept_long(monkeypatch):
    import asyncio

    from finresearch.signals import stock as st

    loads = {"n": 0}

    async def load(sym):
        loads["n"] += 1
        return {"errors": ["results: ConnectError"], "unreachable": ["results"]} if loads["n"] == 1 else \
            {"errors": [], "unreachable": []}  # fmt: skip

    monkeypatch.setattr(st, "SOURCES", st.StockSources(load=load))
    monkeypatch.setattr(st, "_cache", {})
    clock = {"t": 100.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])
    assert asyncio.run(st.inputs("INFY"))["unreachable"] == ["results"]
    clock["t"] += st.NEGATIVE_CACHE_S + 1
    assert asyncio.run(st.inputs("INFY"))["unreachable"] == [] and loads["n"] == 2
    clock["t"] += 600
    asyncio.run(st.inputs("INFY"))
    assert loads["n"] == 2  # complete inputs keep their half hour

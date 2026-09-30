"""Every exchange-backed data set carries a scrip-specific source URL that is exactly the request that produced it
(BSE and NSE adapters, the agents' equity tools, the stock routes and the baseline facts); never an exchange home page.
Offline: requests are recorded, not sent."""

from __future__ import annotations

import contextlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from finresearch.adapters import bse_equity, nse_equity
from finresearch.adapters.bse import BSE_API
from finresearch.adapters.bse_equity import BseEquity, bse_quote_page, bse_source_url
from finresearch.adapters.nse_equity import NseEquity, nse_quote_page, nse_source_url

NSE_FIX = Path(__file__).parent / "fixtures" / "nse" / "equity"
HOMES = {
    "https://www.bseindia.com",
    "https://www.bseindia.com/",
    "https://www.nseindia.com",
    "https://www.nseindia.com/",
}


def _split(url: str) -> tuple[str, dict[str, str]]:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}", dict(parse_qsl(p.query, keep_blank_values=True))


class _BseRecorder:
    """Stands in for BseClient.get_json / http.get and records every request."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.http = self

    async def get_json(self, path, params=None, cache_ttl=None):
        self.calls.append((f"{BSE_API}{path}", dict(params or {})))
        return {}, None

    async def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url, dict(params or {})))

        class R:
            ok, status, text = True, 200, "Date,Open Price\n"

        return R()

    async def aclose(self):
        return None


@pytest.mark.parametrize(("kind", "call"), [
    ("quote", lambda eq: eq.quote("526433")),
    ("announcements", lambda eq: eq.announcements("526433", today=date(2026, 9, 30))),
    ("corporate_actions", lambda eq: eq.corporate_actions("526433")),
    ("integrated_filings", lambda eq: eq.integrated_filings("526433")),
    ("annual_reports", lambda eq: eq.annual_reports("526433")),
    ("shareholding", lambda eq: eq.shareholding("526433")),
])  # fmt: skip
async def test_bse_source_url_is_the_request_made(kind, call):
    rec = _BseRecorder()
    await call(BseEquity(rec))
    kw = {"today": date(2026, 9, 30)} if kind == "announcements" else {}
    assert _split(bse_source_url(kind, "526433", **kw)) == rec.calls[0]
    assert "526433" in bse_source_url(kind, "526433", **kw)


async def test_bse_price_history_source_is_the_csv_requested():
    rec = _BseRecorder()
    with contextlib.suppress(bse_equity.BseError):  # the recorder's CSV has no rows; only the request matters
        await BseEquity(rec).history("526433", date(2026, 9, 1), date(2026, 9, 29))
    got = bse_source_url("history", "526433", start=date(2026, 9, 1), end=date(2026, 9, 29))
    assert _split(got) == rec.calls[0] and "Scode=526433" in got


@pytest.mark.parametrize(("kind", "kw", "call"), [
    ("history", {"start": date(2026, 9, 1), "end": date(2026, 9, 28)},
     lambda eq: eq.history("INFY", date(2026, 9, 1), date(2026, 9, 28))),
    ("announcements", {}, lambda eq: eq.announcements("INFY")),
    ("results", {"period": "Quarterly"}, lambda eq: eq.results("INFY")),
    ("integrated_filings", {}, lambda eq: eq.integrated_filings("INFY")),
    ("shareholding", {}, lambda eq: eq.shareholding("INFY")),
    ("corporate_actions", {}, lambda eq: eq.corporate_actions("INFY")),
    ("annual_reports", {}, lambda eq: eq.annual_reports("INFY")),
])  # fmt: skip
async def test_nse_source_url_is_the_request_made(monkeypatch, kind, kw, call):
    calls: list = []

    async def fake_get(self, symbol, path, params, *, cache_ttl=None):
        calls.append((f"{nse_equity.NSE_BASE}{path}", dict(params)))
        return []

    monkeypatch.setattr(NseEquity, "_get", fake_get)
    await call(NseEquity(object()))
    got = nse_source_url(kind, "INFY", **kw)
    assert _split(got) == calls[0] and "symbol=INFY" in got


def test_quote_pages_are_scrip_specific():
    assert nse_quote_page("INFY") == "https://www.nseindia.com/get-quotes/equity?symbol=INFY"
    assert bse_quote_page("526433") == f"{BSE_API}/getScripHeaderData/w?Debtflag=&scripcode=526433&seriesid="

    class Q:
        page_url = "https://www.bseindia.com/stock-share-price/asm-technologies-ltd/asmtec/526433/"

    assert bse_quote_page("526433", Q()) == Q.page_url


class _FakeNse:
    """NseEquity's interface over the recorded INFY payloads."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def history(self, symbol, start, end, series="EQ"):
        from finresearch.adapters.nse_equity import PriceBar

        rows = json.loads((NSE_FIX / "history_INFY_20260901_20260928.json").read_text())
        rows = rows.get("data", rows) if isinstance(rows, dict) else rows
        return [PriceBar.parse(r) for r in rows]

    async def announcements(self, symbol):
        from finresearch.adapters.nse_equity import Announcement

        return [
            Announcement.parse(r)
            for r in json.loads((NSE_FIX / "announcements_INFY_trimmed.json").read_text())
        ]

    async def corporate_actions(self, symbol):
        from finresearch.adapters.nse_equity import CorporateAction

        return [CorporateAction.parse(r) for r in json.loads((NSE_FIX / "actions_INFY.json").read_text())]

    async def shareholding(self, symbol):
        from finresearch.adapters.nse_equity import Shareholding

        return [Shareholding.parse(r) for r in json.loads((NSE_FIX / "shareholding_INFY.json").read_text())]

    async def results(self, symbol, period="Quarterly"):
        return []

    async def integrated_filings(self, symbol):
        return []


async def test_nse_agent_tools_cite_the_exact_nse_request(monkeypatch):
    from finresearch.mcp_server import server

    monkeypatch.setattr(nse_equity, "NseEquity", lambda *a: _FakeNse())
    outs = {
        "corporate_actions": json.loads(await server.nse_corporate_actions("INFY")),
        "announcements": json.loads(await server.nse_announcements("infy")),
        "shareholding": json.loads(await server.nse_shareholding("INFY")),
        "integrated_filings": json.loads(await server.nse_results_filings("INFY")),
        "history": json.loads(await server.nse_price_history("INFY", "2026-09-01", "2026-09-28")),
    }
    for kind, o in outs.items():
        kw = {"start": date(2026, 9, 1), "end": date(2026, 9, 28)} if kind == "history" else {}
        assert o["source"] == nse_source_url(kind, "INFY", **kw), kind
        assert o["exchange"] == "NSE" and o["symbol"] == "INFY" and o["quote_page"] == nse_quote_page("INFY")
        assert "symbol=INFY" in o["source"] and o["source"] not in HOMES
    assert outs["corporate_actions"]["actions"] and outs["announcements"]["announcements"]
    assert outs["shareholding"]["patterns"] and outs["history"]["bars"]
    assert outs["integrated_filings"]["sources"][1] == nse_source_url("results", "INFY", period="Quarterly")


def test_baseline_facts_cite_scrip_specific_urls():
    from decimal import Decimal

    from finresearch.adapters.nse_equity import CorporateAction, Shareholding
    from finresearch.verify.stock_baseline import stock_facts

    class Q:
        last_price, week52_high, week52_low, issued_shares, as_of = (
            Decimal(1000),
            Decimal(1200),
            Decimal(800),
            None,
            None,
        )
        market_cap, page_url = Decimal(10**11), None

    acts = [CorporateAction(symbol="526433", subject="Interim Dividend - Rs. - 6.0000", ex_date=date(2026, 8, 12),
                            record_date=None, dividend_per_share=Decimal(6))]  # fmt: skip
    holding = [Shareholding(symbol="526433", as_of=date(2026, 6, 30), promoter_pct=Decimal("56.1"), public_pct=None,
                            employee_trusts_pct=None, submitted=None, xbrl=None)]  # fmt: skip
    facts = {
        f.metric: f for f in stock_facts("BSE:526433", Q(), holding, acts, date(2026, 9, 30), exchange="BSE")
    }
    assert facts["last_price"].url == bse_quote_page("526433")
    assert facts["dividend_per_share_ttm"].url == bse_source_url("corporate_actions", "526433")
    assert facts["promoter_holding"].url == bse_source_url("shareholding_summary", "526433")
    assert all(f.url not in HOMES and "526433" in f.url for f in facts.values())
    nse = {f.metric: f for f in stock_facts("INFY", Q(), [], acts, date(2026, 9, 30))}
    assert nse["dividend_per_share_ttm"].url == nse_source_url("corporate_actions", "INFY")

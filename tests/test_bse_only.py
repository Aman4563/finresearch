"""BSE-only stocks beyond the market pages: company creation from a BSE scrip, the daily watch from BSE data,
research facts / discovery / agent tools from BSE, the signal and forensic card from BSE history and XBRL, and the
ledger resolving a BSE forecast on BSE closes. ASM Technologies (BSE:526433) from recorded payloads; offline."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_bse_equity import BSE, XBRL, FakeBse, load, quote
from test_stock_signal import BACKTEST, bars

from finresearch.adapters.bse_equity import parse_annual_reports
from finresearch.adapters.nse_equity import CorporateAction, PriceBar
from finresearch.fincalc.dates import IST
from finresearch.signals import stock as st
from finresearch.suggest.profile import Profile

FIX = Path(__file__).parent / "fixtures"
FY25 = ("https://www.bseindia.com/XBRLFILES/IFIndasDuplicateUploadDocument/"
        "Integrated_Finance_Ind_As_526433_1262025135346_IFIndAs.xml")  # fmt: skip


class AsmBse(FakeBse):
    """FakeBse plus the quote, annual reports and the FY25 annual XBRL; `history_bars` stands in for a longer CSV."""

    def __init__(self, log: list | None = None, history_bars: list | None = None):
        super().__init__(log if log is not None else [])
        self.history_bars = history_bars

    async def quote(self, code):
        return quote(code)

    async def history(self, code, start, end, series="EQ"):
        if self.history_bars is None:
            return await super().history(code, start, end, series)
        self.log.append(("history", code, start, end))
        return [b for b in self.history_bars if start <= b.day <= end]

    async def announcements(self, code):
        from finresearch.adapters.bse_equity import parse_announcements

        # the recorded 120-day window holds no results filing: one in BSE's shape is added
        results = {"NEWSSUB": "ASM Technologies Ltd - 526433 - Financial Results For The Quarter Ended June 30, 2026",
                   "HEADLINE": "Unaudited financial results", "ATTACHMENTNAME": "a1b2c3d4-results.pdf",
                   "CATEGORYNAME": "Result", "NEWS_DT": "2026-08-05T19:40:00"}  # fmt: skip
        return [*await super().announcements(code), *parse_announcements(code, {"Table": [results]})]

    async def annual_reports(self, code):
        return parse_annual_reports(code, load("annual_reports_526433.json"))

    async def fetch_bytes(self, url, cache_ttl=None):
        if url == FY25:
            return (BSE / "results_526433_Consolidated-Mar-Yearly-2025.xml").read_bytes()
        return await super().fetch_bytes(url, cache_ttl)


def test_annual_report_index_keeps_bse_pdfs_and_fixes_backslashes():
    rows = parse_annual_reports("526433", load("annual_reports_526433.json"))
    assert [r.fiscal_label for r in rows[:2]] == ["FY26", "FY25"]
    fy23 = next(r for r in rows if r.to_year == 2023)
    assert (
        fy23.url
        == "https://www.bseindia.com/xml-data/corpfiling/AttachHis/6b9ab487-ca07-44dc-951f-49430ed924b9.pdf"
    )
    bad = {"Table": [{"Year": "2026", "PDFDownload": "https://evil.example.com/x.pdf"},
                     {"Year": "x", "PDFDownload": "https://www.bseindia.com/a.pdf"}]}  # fmt: skip
    assert parse_annual_reports("1", bad) == []


# --------------------------------------------------------------------------- app: company, watch, alerts
@pytest.fixture
def client(env):
    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, Company, MonitorJob, Watch

    with session_scope() as s:
        for m in (Alert, MonitorJob, Watch):
            s.query(m).delete()
        s.query(Company).filter(Company.nse_symbol.in_(["INFY", "TCS"])).update({"nse_symbol": None})
        s.query(Company).filter(Company.bse_code.in_(["526433", "500209"])).update({"bse_code": None})
        for slug in ("infosys", "asm-technologies"):
            s.query(Company).filter(Company.slug == slug).update({"slug": f"{slug}-{uuid.uuid4().hex[:8]}"})

    async def eq_list():
        return (FIX / "nse" / "equity" / "EQUITY_L_head.csv").read_text()

    async def bse_list():
        return load("scrips_trimmed.json")

    with TestClient(create_app(equity_list=eq_list, bse_scrips=bse_list)) as c:
        yield c


def test_add_a_bse_only_company_by_scrip_code(client):
    made = client.post("/api/companies", json={"bse_code": "BSE:526433"}).json()
    assert made["created"] is True and made["exchange"] == "BSE" and made["key"] == "BSE:526433"
    assert made["nse_symbol"] is None and made["bse_code"] == "526433" and made["kind"] == "stock_report"
    slug = made["slug"]
    assert slug.startswith("asm-technologies")
    again = client.post("/api/companies", json={"bse_code": "526433"}).json()
    assert again["created"] is False and again["slug"] == slug
    co = client.get(f"/api/companies/{slug}").json()
    assert co["exchange"] == "BSE" and co["key"] == "BSE:526433" and co["isin"] == "INE867C01010"
    listed = next(c for c in client.get("/api/companies").json() if c["slug"] == slug)
    assert listed["key"] == "BSE:526433" and listed["exchange"] == "BSE"
    # search now links the BSE-only hit to its company
    hit = next(
        h for h in client.get("/api/stocks/search", params={"q": "526433"}).json() if h["key"] == "BSE:526433"
    )
    assert hit["slug"] == slug
    # a dual-listed code adds the NSE company (NSE stays the default exchange), with its BSE code noted
    infy = client.post("/api/companies", json={"bse_code": "500209"}).json()
    assert infy["nse_symbol"] == "INFY" and infy["exchange"] == "NSE" and infy["key"] == "INFY"
    assert client.get(f"/api/companies/{infy['slug']}").json()["bse_code"] == "500209"
    # errors: not a code, unknown code, both or neither identifier
    assert client.post("/api/companies", json={"bse_code": "ASMTEC"}).status_code == 422
    assert client.post("/api/companies", json={"bse_code": "999999"}).status_code == 404
    assert client.post("/api/companies", json={"bse_code": "526433", "nse_symbol": "INFY"}).status_code == 422
    assert client.post("/api/companies", json={}).status_code == 422


def _bar(d, close):
    return PriceBar(day=d, open=None, high=None, low=None, close=Decimal(close), prev_close=None, vwap=None,
                    volume=None, value_inr=None, trades=None)  # fmt: skip


async def test_bse_watch_runs_the_daily_check_from_bse_and_labels_alerts(client):
    from finresearch.adapters.bse_equity import (
        parse_announcements,
        parse_corporate_actions,
        parse_shareholding_index,
        parse_shareholding_summary,
    )
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob, Watch
    from finresearch.monitor.jobs import Deps
    from finresearch.monitor.scheduler import tick

    slug = client.post("/api/companies", json={"bse_code": "526433"}).json()["slug"]
    w = client.post("/api/watches", json={"company": slug, "kind": "stock"}).json()
    assert (
        w["exchange"] == "BSE"
        and w["key"] == "BSE:526433"
        and w["nse_symbol"] is None
        and w["label"] == "BSE 526433"
    )
    # an NSE watch next to it keeps its NSE slots and data
    client.post("/api/companies", json={"nse_symbol": "INFY"})
    client.post("/api/watches", json={"company": "infosys", "kind": "stock"})

    shp = parse_shareholding_index("526433", load("shp_index_526433_trimmed.json"))
    _, pct = parse_shareholding_summary(load("shp_summary_526433.json"))
    shp[0].promoter_pct = pct["promoter"]
    snap = {"bars": [_bar(date(2026, 9, 25), "1000"), _bar(date(2026, 9, 28), "1010")],
            "announcements": parse_announcements("526433", load("announcements_526433_trimmed.json")),
            "actions": parse_corporate_actions("526433", load("actions_526433.json")), "shareholding": shp}  # fmt: skip
    asked: list = []

    async def bse_snapshot(code):
        asked.append(("BSE", code))
        return snap

    async def nse_snapshot(symbol):
        asked.append(("NSE", symbol))
        return {"bars": [], "announcements": [], "actions": [], "shareholding": []}

    deps = Deps(ipo_detail=None, quote=None, stock_snapshot=nse_snapshot, bse_stock_snapshot=bse_snapshot)
    stats = await tick(deps, datetime(2026, 9, 28, 16, 35, tzinfo=IST))
    assert stats["done"] == 2 and set(asked) == {("BSE", "526433"), ("NSE", "INFY")}
    with session_scope() as s:
        slots = {j.slot for j in s.query(MonitorJob)}
        assert s.query(Alert).count() == 0  # the first check records history
    assert "BSE:526433:stock_daily:2026-09-28" in slots and "INFY:stock_daily:2026-09-28" in slots

    snap["bars"].append(_bar(date(2026, 9, 29), "1100"))
    snap["actions"].append(CorporateAction(symbol="526433", subject="Interim Dividend - Rs. - 3.0000",
                                           ex_date=date(2026, 10, 2), record_date=date(2026, 10, 2),
                                           dividend_per_share=Decimal(3)))  # fmt: skip
    snap["shareholding"][0].promoter_pct = pct["promoter"] - Decimal("1.5")
    await tick(deps, datetime(2026, 9, 29, 16, 35, tzinfo=IST))
    with session_scope() as s:
        alerts = {a.kind: a for a in s.query(Alert)}
        watch = s.query(Watch).filter(Watch.bse_code == "526433").one()
        assert watch.meta["stock_initialised"] and watch.exchange == "BSE"
        msgs = {k: (a.message, a.data) for k, a in alerts.items()}
    assert (
        msgs["big_move"][0].startswith("BSE 526433 closed at ₹1100")
        and msgs["big_move"][1]["exchange"] == "BSE"
    )
    assert msgs["ex_date_soon"][0].startswith("BSE 526433: Interim Dividend")
    assert "BSE 526433: promoter holding" in msgs["holding_change"][0]
    feed = client.get("/api/alerts").json()
    assert {(a["key"], a["label"], a["exchange"]) for a in feed} == {("BSE:526433", "BSE 526433", "BSE")}
    rows = {r["key"]: r for r in client.get("/api/watches").json()}
    assert rows["BSE:526433"]["exchange"] == "BSE" and rows["INFY"]["exchange"] == "NSE"


async def test_bse_watch_without_a_bse_source_fails_loudly(client):
    from finresearch.db import session_scope
    from finresearch.db.models import MonitorJob
    from finresearch.monitor.jobs import Deps
    from finresearch.monitor.scheduler import tick

    slug = client.post("/api/companies", json={"bse_code": "526433"}).json()["slug"]
    client.post("/api/watches", json={"company": slug, "kind": "stock"})

    async def nse_snapshot(symbol):  # never asked for a BSE watch
        raise AssertionError(symbol)

    await tick(
        Deps(ipo_detail=None, quote=None, stock_snapshot=nse_snapshot),
        datetime(2026, 9, 28, 16, 35, tzinfo=IST),
    )
    with session_scope() as s:
        job = s.query(MonitorJob).filter(MonitorJob.slot == "BSE:526433:stock_daily:2026-09-28").one()
        assert job.status == "pending" and "no BSE data source" in job.error  # retried, not run on NSE


# --------------------------------------------------------------------------- research: facts, discovery, tools
async def test_bse_baseline_facts_cite_bse_and_the_xbrl():
    from finresearch.orchestrator.stock import fetch_market_bse
    from finresearch.verify.stock_baseline import stock_facts

    m = await fetch_market_bse("526433", equity=AsmBse())
    assert m["exchange"] == "BSE" and m["summary"]["isin"] == "INE867C01010"
    assert m["summary"]["promoter_holding"] == "57.65" and m["summary"]["latest_quarter"]["label"].startswith(
        "Q1"
    )
    facts = {f.metric: f for f in stock_facts("BSE:526433", m["quote"], m["shareholding"], m["actions"],
                                              date(2026, 9, 30), exchange="BSE", results=m["results"])}  # fmt: skip
    assert facts["last_price"].url.startswith("https://www.bseindia.com/stock-share-price/")
    assert facts["last_price"].quote.startswith("LTP ") and facts["week52_high"].quote.startswith(
        "Fifty2WkHigh_adj"
    )
    assert (
        facts["market_cap"].value == m["quote"].market_cap
        and "as published by BSE" in facts["market_cap"].statement
    )
    assert facts["promoter_holding"].value == Decimal("57.65")
    assert facts["revenue_quarter"].value == Decimal("1988160000.0")
    assert facts["eps_quarter"].value == Decimal("18.39") and facts["eps_quarter"].url.startswith(
        "https://www.bseindia.com/XBRLFILES/"
    )
    assert facts["dividend_per_share_ttm"].value == Decimal("21.5")


async def test_bse_stock_discovery_reads_annual_reports_and_results_pdfs():
    from finresearch.ingest.discover import stock_candidates
    from finresearch.ingest.documents import DocKind

    cands = await stock_candidates("526433", exchange="BSE", equity=AsmBse())
    ar = [c for c in cands if c.kind == DocKind.ANNUAL_REPORT]
    assert [c.title for c in ar] == ["Annual report FY26", "Annual report FY25"]
    assert all(c.source == "bse" and c.url.startswith("https://www.bseindia.com/") for c in cands)
    res = [c for c in cands if c.kind == DocKind.FINANCIALS]
    assert [c.url for c in res] == [
        "https://www.bseindia.com/xml-data/corpfiling/AttachLive/a1b2c3d4-results.pdf"
    ]
    assert res[0].title == "Financial results for the period ended 2026-06-30"


async def test_agent_equity_tools_accept_bse(monkeypatch):
    from finresearch.adapters import bse_equity
    from finresearch.mcp_server import server

    monkeypatch.setattr(bse_equity, "BseEquity", lambda *a: AsmBse())
    out = json.loads(await server.nse_corporate_actions("BSE:526433"))
    acts = out["actions"]
    assert any(a["dividend_per_share"] == "2.5" for a in acts)
    same = json.loads(await server.nse_corporate_actions("526433", exchange="BSE"))
    assert same == out and out["symbol"] == "BSE:526433" and out["exchange"] == "BSE"
    # every BSE-backed tool names the exact BSE URL of its data for this scrip, never BSE's home page
    api = "https://api.bseindia.com/BseIndiaAPI/api"
    assert (
        out["source"] == f"{api}/DefaultData/w?Fdate=&Purposecode=&TDate=&ddlcategorys=E&ddlindustrys="
        "&scripcode=526433&segment=0&strSearch=S"
    )
    anns = json.loads(await server.nse_announcements("BSE:526433"))
    assert (
        anns["source"].startswith(f"{api}/AnnSubCategoryGetData/w?") and "strScrip=526433" in anns["source"]
    )
    assert anns["announcements"]
    shp = json.loads(await server.nse_shareholding("BSE:526433"))
    assert shp["source"] == f"{api}/SHPQNewFormat/w?scripcode=526433" and shp["patterns"]
    assert shp["sources"][1] == f"{api}/CorporatesSHPSecuritybeta/w?scripcode=526433&qtrid="
    fil = json.loads(await server.nse_results_filings("BSE:526433"))
    assert fil["source"] == f"{api}/Integratedfinancedata/w?scripcode=526433" and fil["filings"]
    for o in (out, anns, shp, fil):
        assert o["quote_page"] == f"{api}/getScripHeaderData/w?Debtflag=&scripcode=526433&seriesid="
        assert o["source"].rstrip("/") != "https://www.bseindia.com"
    bad = json.loads(await server.nse_shareholding("ASMTEC", exchange="BSE"))
    assert "six-digit scrip code" in bad["error"]
    hist = json.loads(await server.nse_price_history("BSE:526433", "2026-09-01", "2026-09-29"))
    assert hist["exchange"] == "BSE" and hist["symbol"] == "BSE:526433" and hist["bars"]
    # the citation is the exact BSE file read (a live run cited BSE's bare home page before this)
    assert hist["source"] == ("https://api.bseindia.com/BseIndiaAPI/api/StockPriceCSVDownload/w?pageType=0&rbType=D"
                              "&Scode=526433&FDates=01%2F09%2F2026&TDates=29%2F09%2F2026")  # fmt: skip
    url = next(iter(k for k in XBRL if "IFIndAs" in k and "582026193852" in k))
    facts = json.loads(await server.nse_results_facts(url))
    assert facts["consolidated"] is True and facts["periods"] and facts["source"] == url
    assert "error" in json.loads(await server.nse_results_facts("https://www.bseindia.com/some/page.html"))


def test_prompts_name_the_bse_listing():
    from finresearch.agents.roles import ROLES
    from finresearch.agents.runner import RunContext, render

    bse = RunContext(run_id=1, company_slug="asm", company_name="ASM Technologies", bse_code="526433",
                     subject="a listed Indian stock")  # fmt: skip
    system, prompt = render(ROLES["stock_planner"], bse)
    assert "(BSE 526433; BSE-only" in system and 'pass "BSE:526433"' in system
    assert "slug asm, BSE 526433" in prompt and bse.values()["nse_symbol"] == "BSE:526433"
    nse = RunContext(run_id=1, company_slug="infy", company_name="Infosys", nse_symbol="INFY")
    assert "(NSE INFY)" in render(ROLES["stock_planner"], nse)[0]  # unchanged for NSE
    assert "slug infy, NSE INFY)" in render(ROLES["stock_planner"], nse)[1]


# --------------------------------------------------------------------------- signal + forensic
@pytest.fixture
def bse_sources(monkeypatch):
    logged: list = []
    eq = AsmBse(history_bars=bars(800, 0.0012, start=date(2023, 6, 1)))

    async def load(sym):
        assert sym == "BSE:526433"
        return await st._load_live_bse("526433", eq)

    def record(sig, **kw):
        logged.append((sig, kw))
        return 1

    monkeypatch.setattr(st, "SOURCES", st.StockSources(load=load, backtest=lambda: BACKTEST, record=record,
                                                        profile=lambda: Profile(risk_appetite="medium"),
                                                        today=lambda: date(2026, 9, 30)))  # fmt: skip
    monkeypatch.setattr(st, "LOG_FORECASTS", True)
    monkeypatch.setattr(st, "_cache", {})
    return logged


def test_forensic_card_from_bse_xbrl(bse_sources):
    raw = asyncio.run(st.inputs("BSE:526433"))
    assert sorted(raw["annual"]) == [
        date(2025, 3, 31),
        date(2026, 3, 31),
    ]  # both half-yearly filers' full years
    f = st.forensic(raw)
    assert f["exchange"] == "BSE" and f["scrip_code"] == "526433" and f["symbol"] == "ASMTEC"
    assert (
        f["fiscal_year_end"] == "2026-03-31"
        and f["prior_year_end"] == "2025-03-31"
        and f["basis"] == "consolidated"
    )
    assert all(src.startswith("https://www.bseindia.com/XBRLFILES/") for src in f["sources"])
    scores = {s["key"]: s for s in f["scores"]}
    assert set(scores) == {"piotroski", "altman", "beneish", "accruals", "cfo_ebitda"}
    assert scores["piotroski"]["value"] == 7.0 and not scores["piotroski"]["missing"]
    assert any("Mar-2025" in n for n in f["notes"])


def test_signal_for_a_bse_only_stock(bse_sources):
    s = asyncio.run(st.stock_signal("bse:526433", {}))
    assert s.instrument == "BSE:526433" and s.name == "ASM Technologies Ltd"
    assert s.event == st.EVENT_BSE and "BSE closes" in s.event and "NIFTYBEES" in s.event
    assert s.sources[0].startswith("https://www.bseindia.com/")
    assert s.probability == 0.6 and s.action != "NO_SIGNAL"
    assert any("backtested on NSE-listed NIFTY 50 stocks" in c for c in s.caveats)
    assert any("BSE closes (plus cash dividends) against NIFTYBEES on NSE" in c for c in s.caveats)
    ((_sig, kw),) = bse_sources
    assert kw["inputs"]["exchange"] == "BSE" and kw["inputs"]["benchmark"] == "NIFTYBEES"
    assert kw["inputs"]["benchmark_exchange"] == "NSE" and kw["inputs"]["symbol"] == "BSE:526433"
    with pytest.raises(ValueError):
        asyncio.run(st.stock_signal("BSE:12", {}))


def test_half_yearly_bse_filer_keeps_the_pe_factor(bse_sources):
    """ASM files Jun/Sep/Dec quarters but a six-month March: the March filings still give H2 and the fiscal year, so
    the P/E uses FY26's own EPS (41.65) and says why Q1 FY27 cannot extend it (Jul-Sep 2025 is not on file here)."""
    raw = asyncio.run(st.inputs("BSE:526433"))
    periods = {p["label"]: p for p in raw["results"]["periods"]}
    assert set(periods) == {"FY25", "H2 FY25", "FY26", "H2 FY26", "Q1 FY27"}
    assert periods["H2 FY26"]["eps"] == 11.48 and periods["H2 FY26"]["period_start"] == "2025-10-01"
    assert periods["FY26"]["eps"] == 41.65 and periods["FY26"]["source_url"].startswith(
        "https://www.bseindia.com/"
    )
    assert "Q4 FY26" not in {q["label"] for q in raw["results"]["quarters"]}  # still not shown as a quarter
    s = asyncio.run(st.stock_signal("BSE:526433", {}))
    pe = next(f for f in s.factors if f.name == "P/E vs its own history")
    price = float(raw["quote"].last_price)
    assert pe.value == pytest.approx(price / 41.65, abs=0.05)
    assert "last fiscal year EPS to 31 Mar 2026" in pe.explanation
    assert "no separate figures for Jul 2025-Sep 2025" in pe.explanation
    assert "percentile of its daily history since 2025-05-19" in pe.explanation


def test_signal_and_forensic_routes_accept_bse_keys(env, bse_sources):
    from finresearch.api import create_app

    with TestClient(create_app()) as c:
        j = c.get("/api/signals/stock/BSE:526433").json()
        assert j["instrument"] == "BSE:526433" and j["probability"] == 0.6
        f = c.get("/api/stocks/BSE%3A526433/forensic").json()
        assert f["exchange"] == "BSE" and len(f["scores"]) == 5
        assert c.get("/api/stocks/not a symbol/forensic").status_code == 422


def test_since_report_for_a_bse_only_stock(env, monkeypatch):
    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources
    from finresearch.db import session_scope
    from finresearch.db.models import Company, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        s.query(Company).filter(Company.bse_code == "526433").update({"bse_code": None})
        co = get_or_create_company(
            s, f"asm-since-{uuid.uuid4().hex[:6]}", "ASM Technologies Ltd", bse_code="526433"
        )
        run_ = ResearchRun(company_id=co.id, kind="stock_report", status="done", manifest={},
                           finished_at=datetime(2026, 6, 1, 12, tzinfo=UTC))  # fmt: skip
        s.add(run_)
        s.flush()
        run_id = run_.id
    asked: list = []

    async def bse_quote(code):
        asked.append(code)
        return quote(code)

    app = create_app(clock=lambda: datetime(2026, 9, 30, 6, 30, tzinfo=UTC))
    app.state.markets = MarketSources(bse_equity=lambda: AsmBse(), bse_quote=bse_quote)
    with TestClient(app) as c:
        r = c.get("/api/stocks/BSE:526433/since-report").json()
        assert r["run_id"] == run_id and r["symbol"] == "BSE:526433" and r["price"] and asked == ["526433"]
        assert r["new_filings"]["count"] > 0 and not r["errors"]


# --------------------------------------------------------------------------- the ledger
async def test_ledger_resolves_a_bse_forecast_on_bse_closes(env):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.signals.ledger import resolve_excess_return

    asked: list = []

    async def nse_history(sym, start, end):
        asked.append(("NSE", sym))
        assert sym == "NIFTYBEES"
        return [_bar(date(2025, 9, 30), "250"), _bar(date(2026, 9, 30), "275")]

    async def bse_history(code, start, end):
        asked.append(("BSE", code))
        return [_bar(date(2025, 9, 30), "1000"), _bar(date(2026, 9, 30), "1100")]

    async def bse_actions(code):
        return [CorporateAction(symbol=code, subject="Interim Dividend - Rs. - 20.0000", ex_date=date(2026, 2, 11),
                                record_date=None, dividend_per_share=Decimal(20))]  # fmt: skip

    from types import SimpleNamespace

    deps = SimpleNamespace(price_history=nse_history, bse_price_history=bse_history,
                           bse_corporate_actions=bse_actions, corporate_actions=None)  # fmt: skip

    f = Forecast(asset="stock", instrument="BSE:526433", source="signal:stock", event_kind="excess_return_12m",
                 event=st.EVENT_BSE, horizon="12 months", resolve_on=date(2026, 9, 30), action="BUY", method="m",
                 validation_status="rule_based", dedupe_key=f"t-{uuid.uuid4().hex}", created_at=datetime.now(UTC),
                 inputs={"symbol": "BSE:526433", "start_date": "2025-09-30", "benchmark": "NIFTYBEES",
                         "exchange": "BSE"})  # fmt: skip
    with session_scope() as s:
        res = await resolve_excess_return(f, deps, s)
    assert ("BSE", "526433") in asked and ("NSE", "NIFTYBEES") in asked
    assert res.outcome == 1 and res.value == pytest.approx(2.0)  # +12% (10% + ₹20 dividend) vs +10%
    assert "₹1000 on 2025-09-30" in res.note and "NIFTYBEES +10.00%" in res.note


def test_record_run_logs_a_bse_only_report(env):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, Company, ResearchRun
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.signals.ledger import STOCK_EVENT_BSE, record_run

    with session_scope() as s:
        s.query(Company).filter(Company.bse_code == "526433").update({"bse_code": None})
        co = get_or_create_company(
            s, f"asm-run-{uuid.uuid4().hex[:6]}", "ASM Technologies Ltd", bse_code="526433"
        )
        run_ = ResearchRun(company_id=co.id, kind="stock_report", status="done", manifest={},
                           finished_at=datetime(2026, 9, 30, 6, tzinfo=UTC))  # fmt: skip
        s.add(run_)
        s.flush()
        s.add(AgentStep(run_id=run_.id, key="synthesizer", stage="synthesis", role="stock_synthesizer",
                        status="done", output={"verdict": "BUY", "confidence": "medium", "horizon": "3 years"}))  # fmt: skip
        s.flush()
        fid = record_run(s, run_.id)
        from finresearch.db.models import Forecast

        fc = s.get(Forecast, fid)
        assert fc.instrument == "BSE:526433" and fc.event == STOCK_EVENT_BSE and fc.probability == 0.65
        assert fc.inputs["exchange"] == "BSE" and fc.inputs["benchmark"] == "NIFTYBEES"


# --------------------------------------------------------------------------- the stock_report pipeline (fake runner)
async def test_stock_pipeline_for_a_bse_only_company(env, tmp_path, monkeypatch):
    import hashlib

    from test_stock_pipeline import StockRunner

    from finresearch.bridge.limits import LimitTracker
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, Company, Document, ResearchRun
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator import stock as stock_mod
    from finresearch.orchestrator.base import PipelineConfig, create_run
    from finresearch.orchestrator.kinds import pipeline_for

    slug = f"asm-{uuid.uuid4().hex[:8]}"
    txt = tmp_path / "ar.txt"
    txt.write_text("Annual report text")
    with session_scope() as s:
        s.query(Company).filter(Company.bse_code == "526433").update({"bse_code": None})
        co = get_or_create_company(s, slug, "ASM Technologies Ltd", bse_code="526433", isin="INE867C01010")
        s.add(Document(company_id=co.id, kind="ANNUAL_REPORT", title="Annual report FY26", sha256=hashlib.sha256(
            slug.encode()).hexdigest(), local_path=str(txt), text_path=str(txt), bytes=1, pages=1))  # fmt: skip
    run_id = create_run(slug, kind="stock_report")
    asked: list = []
    real = stock_mod.fetch_market_bse

    async def fake_bse(code):
        asked.append(code)
        return await real(code, equity=AsmBse())

    async def no_nse(symbol):
        raise AssertionError("a BSE-only company must not read NSE")

    monkeypatch.setattr(stock_mod, "fetch_market_bse", fake_bse)
    monkeypatch.setattr(stock_mod, "fetch_market", no_nse)
    runner = StockRunner(run_id)
    seen_ctx: list = []
    orig_call = runner.__call__

    async def spy(role, ctx, **extra):
        seen_ctx.append(ctx)
        return await orig_call(role, ctx, **extra)

    pipe = pipeline_for(run_id, runner=spy, tracker=LimitTracker(tmp_path / "limits"),
                        config=PipelineConfig(concurrency=3, render=False))  # fmt: skip
    assert await pipe.run() == "done"
    assert asked == ["526433"]
    assert seen_ctx[0].bse_code == "526433" and seen_ctx[0].values()["nse_symbol"] == "BSE:526433"
    with session_scope() as s:
        facts = {c.metric: c for c in s.query(Claim).filter_by(run_id=run_id, stream="facts")}
        m = s.get(ResearchRun, run_id).manifest
        assert facts["last_price"].statement.endswith("(BSE)") and facts["last_price"].checks == {
            "source": "bse_equity"
        }
        assert facts["market_cap"].status == "verified" and facts["eps_quarter"].citations[0].url.startswith(
            "https://www.bseindia.com/XBRLFILES/"
        )
    assert m["facts"]["market"]["exchange"] == "BSE" and m["facts"]["market"]["bse_code"] == "526433"

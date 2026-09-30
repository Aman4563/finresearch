"""BSE listed equities: the adapter's parsers on recorded BSE payloads (30-Sep-2026), the NSE/BSE listing merge,
and the stock routes for a BSE-only stock (ASM Technologies, 526433) and a dual-listed one (Infosys, INFY / 500209).
Nothing here touches the network."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from finresearch.adapters.bse import BseClient, BseError
from finresearch.adapters.bse_equity import (
    BseEquity,
    bse_dividend_per_share,
    build_quote,
    merge_listings,
    official_file,
    parse_announcements,
    parse_bhavcopy,
    parse_corporate_actions,
    parse_integrated_financials,
    parse_price_csv,
    parse_scrip_list,
    parse_shareholding_index,
    parse_shareholding_summary,
    scrip_code_of,
    search_listings,
)
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse_equity import parse_equity_list
from finresearch.api.markets import MarketSources

FIX = Path(__file__).parent / "fixtures"
BSE = FIX / "bse" / "equity"
TODAY = date(2026, 9, 30)


def load(name: str):
    return json.loads((BSE / name).read_text())


def text(name: str) -> str:
    return (BSE / name).read_text()


def listings():
    nse = parse_equity_list((FIX / "nse" / "equity" / "EQUITY_L_head.csv").read_text())
    return merge_listings(nse, parse_scrip_list(load("scrips_trimmed.json")))


def quote(code: str):
    return build_quote(code, load(f"header_{code}.json"), load(f"comheader_{code}.json"),
                       load(f"trading_{code}.json"), load(f"highlow_{code}.json"))  # fmt: skip


# --------------------------------------------------------------------------- parsers
def test_scrip_master_and_quote():
    scrips = {s.code: s for s in parse_scrip_list(load("scrips_trimmed.json"))}
    assert scrips["526433"].symbol == "ASMTEC" and scrips["526433"].isin == "INE867C01010"
    assert scrips["526433"].group == "B" and scrips["544937"].symbol == "NSE"
    q = quote("526433")
    assert q.symbol == "ASMTEC" and q.company == "ASM Technologies Ltd" and q.isin == "INE867C01010"
    assert q.last_price is not None and q.previous_close is not None and q.as_of is not None
    trading = load("trading_526433.json")
    crore = Decimal(trading["MktCapFull"].replace(",", ""))
    assert q.market_cap == crore * 10_000_000  # BSE publishes Rs crore; the API speaks rupees
    hl = load("highlow_526433.json")
    assert q.week52_high == Decimal(hl["Fifty2WkHigh_adj"]) and q.week52_low == Decimal(hl["Fifty2WkLow_adj"])
    assert q.page_url and q.page_url.startswith("https://www.bseindia.com/stock-share-price/")
    assert build_quote("999999", {"Header": {}}, None, None, None) is None  # no quote: not listed


def test_price_history_csv_matches_the_bhavcopy():
    """The history API and the day's bhavcopy are two BSE publications of the same trades: INFY and ASM on
    29-Sep-2026 agree to the paisa and the share."""
    bhav = {r.code: r for r in parse_bhavcopy(text("bhavcopy_20260929_trimmed.csv"))}
    assert len(bhav) == 9 and bhav["544937"].symbol == "NSE" and bhav["526433"].group == "B"
    for code in ("500209", "526433"):
        bars = parse_price_csv(text(f"price_history_{code}_202609.csv"))
        assert [b.day for b in bars] == sorted(b.day for b in bars) and bars[0].day >= date(2026, 9, 1)
        last, row = bars[-1], bhav[code]
        assert last.day == row.day == date(2026, 9, 29)
        assert (last.open, last.high, last.low, last.close) == (row.open, row.high, row.low, row.close)
        assert last.volume == row.volume and last.trades == row.trades and last.value_inr == row.value_inr
        assert (
            last.prev_close == bars[-2].close == row.prev_close
        )  # previous row's close = BSE's previous close
    infy = bhav["500209"]
    assert infy.close == Decimal("1005.50") and infy.volume == Decimal(394600)
    with pytest.raises(BseError):
        parse_price_csv("<!DOCTYPE html><html>")
    with pytest.raises(BseError):
        parse_bhavcopy("<!DOCTYPE html><html>")


def test_announcements_actions_and_dividends():
    anns = parse_announcements("526433", load("announcements_526433_trimmed.json"))
    assert anns and all(a.at and a.at.tzinfo for a in anns)
    assert anns == sorted(anns, key=lambda a: a.at, reverse=True)
    with_pdf = [a for a in anns if a.attachment]
    assert with_pdf and all(
        a.attachment.startswith("https://www.bseindia.com/xml-data/corpfiling/AttachLive/") for a in with_pdf
    )
    assert bse_dividend_per_share("Interim Dividend - Rs. - 6.0000") == Decimal("6.0000")
    assert bse_dividend_per_share("Bonus issue 1:1") is None
    acts = parse_corporate_actions("526433", load("actions_526433.json"))
    by_ex = {a.ex_date: a for a in acts}
    assert by_ex[date(2026, 8, 12)].dividend_per_share == 6
    assert by_ex[date(2026, 7, 29)].dividend_per_share == 12
    assert by_ex[date(2025, 8, 22)].dividend_per_share == 1  # BSE's "last five" table drops this one
    ttm = sum(
        (a.dividend_per_share or 0) for a in acts if a.ex_date and date(2025, 9, 30) < a.ex_date <= TODAY
    )
    assert ttm == Decimal("21.5")  # 1 (14-Nov-25) + 2.5 + 12 + 6; the Rs 3 of 19-Sep-25 is over a year old


def test_integrated_filings_one_per_xbrl_file():
    filings = parse_integrated_financials("526433", load("integrated_financials_526433.json"))
    urls = [f.xbrl for f in filings]
    assert len(urls) == len(set(urls))  # the Hly / Yearly / NineMths views of one filing collapse into one
    assert all(u.startswith("https://www.bseindia.com/XBRLFILES/") and u.endswith(".xml") for u in urls)
    assert all(official_file(u) for u in urls)
    jun = [f for f in filings if f.period_end == date(2026, 6, 30)]
    assert {f.consolidated for f in jun} == {True, False}
    r = next(f for f in jun if f.consolidated).as_result_filing()
    assert r.source == "bse_integrated_filing" and r.period_from == date(2026, 4, 1)
    assert r.ixbrl and r.ixbrl.endswith(".html")
    dec = next(f for f in filings if f.period_end == date(2025, 12, 31) and f.consolidated)
    assert dec.xbrl.endswith("_3112026192731_IFIndAs.xml")
    assert any(f.period_end == date(2026, 3, 31) for f in filings)  # March appears only as Hly / Yearly rows


def test_shareholding_index_and_summary():
    rows = parse_shareholding_index("526433", load("shp_index_526433_trimmed.json"))
    assert rows[0].as_of == date(2026, 6, 30) and rows[1].as_of == date(2026, 3, 31)
    assert rows[0].xbrl == "https://www.bseindia.com/XBRLFILES/SHPXBRLDataXML/526433_2172026103321_SHP.xml"
    assert any(r.as_of == date(2025, 10, 9) for r in rows)  # an event filing keeps its own date
    end, pct = parse_shareholding_summary(load("shp_summary_526433.json"))
    assert (
        end == date(2026, 6, 30) and pct["promoter"] == Decimal("57.65") and pct["public"] == Decimal("42.35")
    )


def test_listing_merge_by_isin_and_search():
    idx = listings()
    infy = idx.by_nse("INFY")
    assert infy.exchange == "both" and infy.bse_code == "500209" and infy.key == "INFY"
    asm = idx.by_code("526433")
    assert asm.exchange == "BSE" and asm.key == "BSE:526433" and asm.nse_symbol is None
    # BSE's scrip id "NSE" is National Stock Exchange of India Ltd, BSE-only: keyed by code, never by that id
    nse_ltd = idx.by_code("544937")
    assert nse_ltd.key == "BSE:544937" and idx.by_nse("NSE") is None
    assert [r.key for r in search_listings(idx, "asm tech")] == ["BSE:526433"]
    assert search_listings(idx, "526433")[0].key == "BSE:526433"
    assert search_listings(idx, "BSE:500209")[0].key == "INFY"
    assert search_listings(idx, "INFY")[0].key == "INFY"
    assert scrip_code_of("bse:526433") == "526433" and scrip_code_of("INFY") is None


def _bse_transport(routes: dict[str, bytes]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        for part, body in routes.items():
            if part in str(req.url):
                return httpx.Response(200, content=body)
        return httpx.Response(200, content=b"<!DOCTYPE html><html>BSE home</html>")  # the site's SPA shell

    return httpx.MockTransport(handler)


async def _nosleep(_s: float) -> None:
    return None


async def test_client_guards_the_html_shell():
    """BSE answers missing files with its HTML shell and HTTP 200: never an XBRL, a bhavcopy or a history."""
    routes = {"StockPriceCSVDownload": (BSE / "price_history_526433_202609.csv").read_bytes(),
              "BhavCopy_BSE_CM_0_0_0_20260929": (BSE / "bhavcopy_20260929_trimmed.csv").read_bytes(),
              "526433_2172026103321_SHP.xml": (BSE / "shp_526433_June-2026.xml").read_bytes()}  # fmt: skip
    http = PoliteClient(transport=_bse_transport(routes), cache_dir=None, sleep=_nosleep)
    async with BseEquity(BseClient(http)) as eq:
        bars = await eq.history("526433", date(2026, 9, 1), date(2026, 9, 29))
        assert bars[-1].day == date(2026, 9, 29)
        assert len(await eq.bhavcopy(date(2026, 9, 29))) == 9
        assert await eq.bhavcopy(date(2026, 9, 27)) is None  # Sunday: not published
        ok = "https://www.bseindia.com/XBRLFILES/SHPXBRLDataXML/526433_2172026103321_SHP.xml"
        assert (await eq.fetch_bytes(ok)).lstrip(b"\xef\xbb\xbf").startswith(b"<?xml")
        with pytest.raises(BseError, match="not XBRL"):
            await eq.fetch_bytes("https://www.bseindia.com/XBRLFILES/SHPXBRLDataXML/missing_SHP.xml")
        with pytest.raises(BseError, match="not a BSE filing URL"):
            await eq.fetch_bytes("https://evil.example.com/XBRLFILES/x.xml")
    await http.aclose()


# --------------------------------------------------------------------------- routes
XBRL = {url: BSE / name for url, name in load("xbrl_files.json").items()}
XBRL["https://www.bseindia.com/XBRLFILES/IFIndasDuplicateUploadDocument/"
     "Integrated_Finance_Ind_As_526433_952026235451_IFIndAs.xml"] = BSE / "results_526433_Consolidated-Mar-Yearly-2026.xml"  # fmt: skip


class FakeBse:
    """BseEquity's interface over the recorded ASM Technologies payloads."""

    exchange = "BSE"
    history_window_days = 3660
    answers_full_range = True

    def __init__(self, log: list):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def history(self, code, start, end, series="EQ"):
        self.log.append(("history", code, start, end))
        return [b for b in parse_price_csv(text(f"price_history_{code}_202609.csv")) if start <= b.day <= end]

    async def announcements(self, code):
        return parse_announcements(code, load("announcements_526433_trimmed.json"))

    async def corporate_actions(self, code):
        return parse_corporate_actions(code, load("actions_526433.json"))

    async def integrated_filings(self, code, kind="Integrated Filing- Financials"):
        return parse_integrated_financials(code, load("integrated_financials_526433.json"))

    async def results(self, code, period="Quarterly"):
        return []

    async def shareholding(self, code):
        rows = parse_shareholding_index(code, load("shp_index_526433_trimmed.json"))
        end, pct = parse_shareholding_summary(load("shp_summary_526433.json"))
        rows[0].promoter_pct, rows[0].public_pct = pct["promoter"], pct["public"]
        assert rows[0].as_of == end
        return rows

    async def fetch_bytes(self, url, cache_ttl=None):
        self.log.append(("xbrl", url))
        if url not in XBRL:
            raise BseError(f"HTTP 404 for {url}")
        return XBRL[url].read_bytes()


@pytest.fixture
def bse_app(env):
    from finresearch.api import create_app

    log: list = []

    async def bse_quote(code):
        log.append(("quote", code))
        return quote(code)

    async def nse_quote(symbol):
        from finresearch.adapters.nse import Quote

        return Quote.parse(json.loads((FIX / "nse" / "quote_INFY_20260928.json").read_text()))

    async def get_listings():
        return listings()

    app = create_app()
    app.state.markets = MarketSources(bse_equity=lambda: FakeBse(log), bse_quote=bse_quote, quote=nse_quote,
                                      listings=get_listings, today=lambda: TODAY)  # fmt: skip
    with TestClient(app) as c:
        yield c, log


def test_bse_only_stock_overview_and_history(bse_app):
    c, log = bse_app
    r = c.get("/api/stocks/BSE:526433/overview")
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["exchange"] == "BSE" and o["key"] == "BSE:526433" and o["scrip_code"] == "526433"
    assert o["symbol"] == "ASMTEC" and o["source"].startswith("https://www.bseindia.com/stock-share-price/")
    assert o["listing"]["exchange"] == "BSE" and o["listing"]["nse_symbol"] is None
    q = o["quote"]
    assert q["exchange"] == "BSE" and q["isin"] == "INE867C01010" and q["market_cap"]
    assert Decimal(o["dividends"]["ttm_per_share"]) == Decimal("21.5")
    assert o["corporate_actions"][0]["ex_date"] == "2026-08-12"
    assert o["announcements"] and o["announcements"][0]["attachment"].startswith("https://www.bseindia.com/")
    assert o["shareholding"][0]["promoter_pct"] == 57.65
    assert (
        c.get("/api/stocks/bse:526433/overview").json()["key"] == "BSE:526433"
    )  # cached, case-insensitive key

    h = c.get("/api/stocks/BSE:526433/history", params={"days": 1825}).json()
    assert h["exchange"] == "BSE" and h["bars"][-1]["date"] == "2026-09-29"
    assert sum(1 for x in log if x[0] == "history") == 1  # BSE answers five years in one request


def test_bse_results_and_shareholding_from_xbrl(bse_app):
    c, _ = bse_app
    r = c.get("/api/stocks/BSE:526433/results", params={"quarters": 4}).json()
    q1 = next(x for x in r["quarters"] if x["period_end"] == "2026-06-30")
    assert q1["source"] == "bse_integrated_filing" and q1["consolidated"] is True
    assert q1["revenue"] == 1988160000.0 and q1["profit"] == 268230000.0 and q1["eps"] == 18.39
    assert q1["source_url"].startswith("https://www.bseindia.com/XBRLFILES/") and q1["source_url"].endswith(
        ".html"
    )
    # ASM's March filing reports the half year Oct-Mar as its current period: never shown as a quarter, but the
    # full year is
    assert all(x["period_end"] != "2026-03-31" for x in r["quarters"])
    assert any("not a quarter" in e for e in r["errors"])
    fy26 = next(x for x in r["annual"] if x["period_end"] == "2026-03-31")
    assert fy26["label"] == "FY26" and fy26["revenue"] > 0
    assert r["sources"][0]["name"].startswith("BSE")

    s = c.get("/api/stocks/BSE:526433/shareholding", params={"quarters": 2}).json()
    assert s["exchange"] == "BSE" and [x["as_of"] for x in s["quarters"]] == ["2026-03-31", "2026-06-30"]
    jun = s["quarters"][-1]["categories"]
    assert jun["promoter"] == 57.65 and jun["retail"] == 16.96 and jun["hni"] == 18.78


def test_exchange_switch_for_a_dual_listed_stock(bse_app):
    c, _ = bse_app
    ov = c.get("/api/stocks/INFY/overview", params={"exchange": "BSE"}).json()
    assert ov["exchange"] == "BSE" and ov["key"] == "BSE:500209" and ov["symbol"] == "INFY"
    assert ov["listing"]["exchange"] == "both" and ov["listing"]["nse_symbol"] == "INFY"
    lq = c.get("/api/stocks/INFY/quote", params={"exchange": "BSE"}).json()
    assert lq["exchange"] == "BSE" and lq["quote"]["scrip_code"] == "500209"
    assert c.get("/api/stocks/BSE:500209/quote").json()["quote"]["symbol"] == "INFY"
    nse = c.get("/api/stocks/INFY/quote").json()
    assert nse["exchange"] == "NSE" and nse["quote"]["exchange"] == "NSE"
    assert nse["source"] == "https://www.nseindia.com/get-quotes/equity?symbol=INFY"
    # errors: an NSE-only symbol has no BSE side, a BSE-only code has no NSE side, and only NSE / BSE exist
    assert c.get("/api/stocks/20MICRONS/overview", params={"exchange": "BSE"}).status_code == 404
    assert c.get("/api/stocks/BSE:526433/overview", params={"exchange": "NSE"}).status_code == 404
    assert c.get("/api/stocks/INFY/overview", params={"exchange": "MCX"}).status_code == 422


def test_search_merges_nse_and_bse(env):
    from finresearch.api import create_app

    async def eq_list():
        return (FIX / "nse" / "equity" / "EQUITY_L_head.csv").read_text()

    async def bse_list():
        return load("scrips_trimmed.json")

    with TestClient(create_app(equity_list=eq_list, bse_scrips=bse_list)) as c:
        hits = c.get("/api/stocks/search", params={"q": "asm"}).json()
        asm = next(h for h in hits if h["bse_code"] == "526433")
        assert asm["key"] == "BSE:526433" and asm["exchange"] == "BSE" and asm["exchanges"] == ["BSE"]
        infy = c.get("/api/stocks/search", params={"q": "infosys"}).json()[0]
        assert (
            infy["key"] == infy["symbol"] == "INFY"
            and infy["exchange"] == "both"
            and infy["bse_code"] == "500209"
        )
        assert infy["isin"] == "INE009A01021" and infy["listed"] == "1995-02-08" and infy["bse_error"] is None

    async def bse_down():
        raise RuntimeError("BSE HTTP 403")

    with TestClient(create_app(equity_list=eq_list, bse_scrips=bse_down)) as c:
        infy = c.get("/api/stocks/search", params={"q": "infy"}).json()[0]
        assert infy["key"] == "INFY" and infy["exchange"] == "NSE" and "403" in infy["bse_error"]


def test_bse_filing_checks():
    """Filed figures are shown only when they add up: a six-month "quarter" and a year-to-date smaller than the
    quarter inside it (ASM's Sep-2025 filing) are caught; an owners' profit filed as 0 falls back to the period's."""
    from finresearch.adapters.xbrl import PeriodFacts
    from finresearch.api.markets import _bse_filing_problem, _fill_owner_profit

    d = Decimal
    half = PeriodFacts("OneD", date(2025, 10, 1), date(2026, 3, 31), {"revenue_from_operations": d(1)})
    assert _bse_filing_problem(half, None) == "half_year"
    q = PeriodFacts("OneD", date(2025, 7, 1), date(2025, 9, 30), {"revenue_from_operations": d(2773750000)})
    ytd = PeriodFacts(
        "FourD", date(2025, 4, 1), date(2025, 9, 30), {"revenue_from_operations": d(1544600000)}
    )
    assert "swapped" in _bse_filing_problem(q, ytd)
    ytd.facts["revenue_from_operations"] = d(4002900000)
    assert _bse_filing_problem(q, ytd) is None
    errors: list[str] = []
    dec = PeriodFacts("OneD", date(2025, 10, 1), date(2025, 12, 31),
                      {"profit_attributable_to_owners": d(0), "profit_for_period": d(93120000)})  # fmt: skip
    _fill_owner_profit(dec, errors, date(2025, 12, 31))
    assert dec.facts["profit_attributable_to_owners"] == d(93120000) and "filed as 0" in errors[0]

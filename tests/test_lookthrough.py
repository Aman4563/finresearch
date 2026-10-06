"""Fund look-through: golden formulas, AMC file parsing (trimmed real files), discovery, the store, and the API.

Fixtures in tests/fixtures/amc/ are the fund houses' own Aug/Jul-2026 monthly portfolio files, downloaded
30-Sep-2026 and trimmed (a subset of rows, values only) so they stay small: PPFAS (Parag Parikh Flexi Cap), Axis
Midcap, Nippon India (Growth Mid Cap, ETF Nifty 50 BeES, ETF Nifty Midcap 150) and DSP (a zip). Offline only.
"""

from __future__ import annotations

import base64
import io
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.amc_portfolio import (
    ALLOWED_HOSTS,
    AMC_SOURCES,
    AmcPortfolioError,
    _num,
    check_url,
    discover_links,
    parse_amfi_cap_list,
    parse_as_of,
    parse_file,
    pick_links,
    scheme_key,
)
from finresearch.fincalc.lookthrough import (
    DirectInput,
    FundInput,
    FundLine,
    active_share,
    drift,
    hhi,
    lookthrough,
    overlap,
    overlap_matrix,
    style_series,
)

FIX = Path(__file__).parent / "fixtures" / "amc"


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def by_isin(p, isin):
    return next(h for h in p.holdings if h.isin == isin)


# ----------------------------------------------------------------------------------------------- golden formulas
def test_overlap_matches_sebi_annexure_1a_worked_example():
    # SEBI Master Circular for Mutual Funds, Annexure 1A: scrips P, Q, R, S, X, Y, Z -> overlap 45 %
    a = {"P": D(10), "Q": D(15), "R": D(20), "S": D(10), "Y": D(25), "Z": D(20)}
    b = {"P": D(25), "Q": D(30), "R": D(10), "X": D(25), "Y": D(10)}
    o = overlap(a, b)
    assert o.overlap == D(45)
    assert o.common == 4
    assert [x[0] for x in o.items] == ["Q", "P", "R", "Y"]  # largest min first; ties at 10 % by ISIN
    assert overlap(b, a).overlap == D(45)  # symmetric


def test_overlap_edge_cases():
    a = {"X": D(40), "Y": D(30), "Z": D(30)}
    b = {"X": D(20), "Y": D(50), "W": D(30)}
    assert (overlap(a, b).overlap, overlap(a, b).common) == (D(50), 2)
    assert overlap(a, {"Q": D(100)}).overlap == 0  # disjoint
    assert overlap(a, a).overlap == D(100)  # self = the fund's own equity weight
    m = overlap_matrix({"A": a, "B": b, "C": a})
    assert list(m) == [("A", "B"), ("A", "C"), ("B", "C")]
    assert m[("A", "C")].overlap == 100


def test_active_share_and_drift_goldens():
    a = {"X": D(40), "Y": D(30), "Z": D(30)}
    b = {"X": D(20), "Y": D(50), "W": D(30)}
    assert active_share(a, b) == D(50)  # ½(20 + 20 + 30 + 30)
    assert active_share(a, a) == 0
    assert active_share(a, {"Q": D(100)}) == 100
    # 90 % equity with the index's mix: cash does not count as active (both sides re-scaled to 100 % equity)
    assert active_share({"X": D(45), "Y": D(45)}, {"X": D(50), "Y": D(50)}) == 0
    assert active_share({}, b) is None
    assert drift(a, a) == 0 and drift(a, {"Q": D(1)}) == 100
    assert hhi({"a": D(50), "b": D(50)}) == (D("0.5"), D(2))


def test_lookthrough_aggregation_reconciles_every_rupee():
    caps = {"X": "Large Cap", "Y": "Mid Cap"}
    direct = [DirectInput("X", "X Bank", D(10000), sector="Private Sector Bank"),
              DirectInput("Y", "Y Tech", D(5000), sector="Computers - Software"),
              DirectInput("SGB", "Gold bond", D(3000), equity=False)]  # fmt: skip
    f1 = FundInput("Fund 1", D(100000), [FundLine("X", "X Bank", D(40), "equity", "Banks"),
                                          FundLine("Y", "Y Tech", D(30), "equity", "IT - Software"),
                                          FundLine("W", "W Hedged", D(5), "arbitrage", "Finance"),
                                          FundLine("D", "D CD", D(10), "debt", "CRISIL A1+")])  # fmt: skip
    f2 = FundInput("Fund 2", D(50000), [FundLine("X", "X Bank", D(20), "equity", "Banks"),
                                         FundLine("Y", "Y Tech", D(50), "equity", "IT - Software"),
                                         FundLine("USZ000000001", "Z Inc", D(20), "foreign_equity", "Software")])  # fmt: skip
    f3 = FundInput("Fund 3", D(20000), None)  # no holdings file
    ex = lookthrough(direct, [f1, f2, f3], cap_of=caps.get)
    assert ex.total == D(188000)
    assert ex.check() == 0  # the buckets add up exactly to the portfolio value
    assert ex.buckets == {"equity": D(120000), "foreign_equity": D(10000), "arbitrage": D(5000), "debt": D(10000),
                          "remainder": D(20000), "no_file": D(20000), "not_equity_direct": D(3000)}  # fmt: skip
    got = {s.key: (s.value, s.sector, s.cap, s.routes) for s in ex.stocks}
    assert got["Y"] == (
        D(60000),
        "IT - Software",
        "Mid Cap",
        {"Direct": D(5000), "Fund 1": D(30000), "Fund 2": D(25000)},
    )
    assert got["X"][0:3] == (D(60000), "Banks", "Large Cap")  # the funds' industry label wins over NSE's
    assert got["USZ000000001"][2] == "Foreign"
    assert [s.key for s in ex.stocks] == ["X", "Y", "USZ000000001"]  # ties by key
    assert ex.equity == D(130000)
    assert ex.sectors == {"Banks": D(60000), "IT - Software": D(60000), "Software": D(10000)}
    assert ex.caps == {"Large Cap": D(60000), "Mid Cap": D(60000), "Foreign": D(10000)}
    assert ex.redundancy.quantize(D("0.0001")) == D(
        "92.3077"
    )  # X and Y reached by >= 2 routes: 120000 / 130000


def test_style_series_months_ordered_with_drift():
    m1 = [FundLine("X", "X", D(50), "equity", "Banks"), FundLine("Y", "Y", D(40), "equity", "IT")]
    m2 = [FundLine("X", "X", D(45), "equity", "Banks"), FundLine("Z", "Z", D(45), "equity", "Auto")]
    s = style_series([("2026-08", m2), ("2026-07", m1)], cap_of={"X": "Large Cap"}.get)
    assert [x.month for x in s] == ["2026-07", "2026-08"]
    assert s[0].drift is None and s[0].equity_pct == 90
    # normalised: Jul X 55.56 Y 44.44, Aug X 50 Z 50 -> ½(5.56 + 44.44 + 50) = 50
    assert s[1].drift.quantize(D("0.01")) == D("50.00")
    assert s[1].caps == {"Large Cap": D(50), "Unclassified": D(50)}


# ----------------------------------------------------------------------------------------------- parser
def test_ppfas_file_sections_and_units():
    [p] = parse_file(fx("ppfas_ppfcf_2026-08.xlsx"), "ppfcf.xlsx")
    assert (p.scheme_name, p.as_of, p.benchmark) == (
        "Parag Parikh Flexi Cap Fund",
        date(2026, 8, 31),
        "Nifty 500 TRI",
    )
    assert p.key == "parag parikh flexicap fund"
    h = by_isin(p, "INE040A01034")
    assert (h.name, h.industry, h.quantity, h.value_lakh, h.weight, h.kind) == (
        "HDFC Bank Limited",
        "Banks",
        D(158729224),
        D("1125390.2"),
        D("7.6300"),
        "equity",
    )  # fraction x 100 = percent
    assert by_isin(p, "INE296A01032").kind == "arbitrage"  # Bajaj Finance: long leg of an arbitrage, hedged
    assert by_isin(p, "INE041025011").kind == "reit_invit"  # Embassy Office Parks REIT
    assert by_isin(p, "US02079K3059").kind == "foreign_equity"  # Alphabet
    assert by_isin(p, "INE237AD6109").kind == "debt"  # a certificate of deposit
    assert by_isin(p, "IN002025Z294").kind == "govt"  # a T-bill
    assert by_isin(p, "INF879O01068").kind == "mf_units"  # liquid-fund parking
    # the derivatives table after GRAND TOTAL (short futures) is not read as holdings
    assert not any("Future" in h.name or "Option" in h.name for h in p.holdings)
    assert p.grand_total_lakh == D("14740450.66") and p.warnings == []


def test_axis_nippon_dsp_layouts():
    [ax] = parse_file(fx("axis_midcap_2026-08.xlsx"), "axis.xlsx")
    assert (ax.scheme_name, ax.benchmark, ax.as_of) == (
        "Axis Midcap Fund",
        "BSE MIDCAP 150 TRI",
        date(2026, 8, 31),
    )
    assert (by_isin(ax, "INE171A01029").weight, by_isin(ax, "INE171A01029").industry) == (D("4.400"), "Banks")
    nip = {p.sheet: p for p in parse_file(fx("nippon_2026-08.xlsx"), "NIMF-MONTHLY-PORTFOLIO-31-Aug-26.xls")}
    gf = nip["GF"]  # ISIN before the name; date "August 31,2026" without a space
    assert (gf.scheme_name, gf.as_of, gf.benchmark) == ("Nippon India Growth Mid Cap Fund", date(2026, 8, 31),
                                                        "NIFTY MIDCAP 150 TRI")  # fmt: skip
    assert (by_isin(gf, "INE949L01017").name, by_isin(gf, "INE949L01017").weight) == ("AU Small Finance Bank Limited",
                                                                                      D("2.9300"))  # fmt: skip
    assert len(nip["NB"].holdings) == 50 and abs(sum(h.weight for h in nip["NB"].holdings) - 100) < 1
    dsp = {
        p.sheet: p for p in parse_file(fx("dsp_2026-08.zip"), "dsp.zip")
    }  # a zip of xlsx; 'Sr. No.' column
    fc = dsp["Flexi Cap"]
    assert (fc.scheme_name, fc.benchmark) == ("DSP Flexi Cap Fund", "Nifty 500 TRI")
    assert by_isin(fc, "INE090A01021").weight == D("9.5700")
    kinds = {h.kind for h in dsp["Aggressive Hybrid"].holdings}
    assert {"equity", "debt", "govt"} <= kinds


def synthetic(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_percent_scale_other_headers_and_errors():
    # a fourth layout: percent weights, "% to NAV", "Security Name", date as dd/mm/yyyy, GRAND TOTAL 100
    data = synthetic([["Example Equity Fund (An open ended equity scheme)"], ["Portfolio as at 31/08/2026"], [],
                      ["Security Name", "ISIN", "Sector", "Qty", "Market Value (Rs Lakh)", "% to NAV"],
                      ["Equity Shares"], ["Alpha Ltd", "INE000A01011", "Banks", 10, 50, 55.5],
                      ["Beta Ltd", "INE000B01011", "Power", 5, 40, "40.00%"], ["TREPS", None, None, None, 4, 4.5],
                      ["Grand Total", None, None, None, 94, 100]])  # fmt: skip
    [p] = parse_file(data, "x.xlsx")
    assert (p.scheme_name, p.as_of) == ("Example Equity Fund", date(2026, 8, 31))
    # '40.00%' text in a percent-scaled column is 40 %, like its plain-number neighbours (it was read 100x too small)
    assert [h.weight for h in p.holdings] == [D("55.5"), D("40.0000")]
    assert (_num("$0.00%"), _num("*"), _num("(1,234.5)"), _num("NIL")) == (0, 0, D("-1234.5"), None)
    # no GRAND TOTAL row: the scale is inferred and the sheet says so
    [q] = parse_file(synthetic([["F"], ["Name", "ISIN", "% to Net Assets"], ["A", "INE000A01011", 0.6],
                                ["B", "INE000B01011", 0.3]]), "y.xlsx")  # fmt: skip
    assert [h.weight for h in q.holdings] == [D("60.0"), D("30.0")] and q.warnings
    with pytest.raises(AmcPortfolioError, match=r"old-format \.xls"):
        parse_file(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100, "old.xls")
    with pytest.raises(AmcPortfolioError, match="not an Excel"):
        parse_file(b"%PDF-1.7", "x.pdf")
    with pytest.raises(AmcPortfolioError, match="no portfolio table"):
        parse_file(synthetic([["nothing here"]]), "z.xlsx")


def test_names_dates_and_keys():
    assert (
        scheme_key("Nippon India Growth Mid Cap Fund") == "nippon india growth midcap fund"
    )  # 'Growth' is kept
    assert scheme_key("Axis Midcap Fund - Direct Plan - Growth Option") == "axis midcap fund"
    assert scheme_key("Axis Mid Cap Fund") == scheme_key("AXIS MIDCAP FUND")
    assert scheme_key("Mirae Asset Large & Midcap Fund") == "mirae asset large and midcap fund"
    assert parse_as_of("Monthly Portfolio Statement as on August 31,2026") == date(2026, 8, 31)
    assert parse_as_of("Portfolio as on 31st Aug 2026") == date(2026, 8, 31)


def test_amfi_cap_list_boundaries():
    cl = parse_amfi_cap_list(fx("amfi_cap_list_2025-12.xlsx"))
    assert cl["as_of"] == "2025-12-31"
    ranks = {v[0]: v[1] for v in cl["by_isin"].values()}
    assert (ranks[100], ranks[101], ranks[250], ranks[251]) == (
        "Large Cap",
        "Mid Cap",
        "Mid Cap",
        "Small Cap",
    )
    assert cl["by_isin"]["INE040A01034"] == [2, "Large Cap"]  # HDFC Bank


# ----------------------------------------------------------------------------------------------- discovery
def test_discovery_from_real_page_extracts():
    pp = discover_links("ppfas", (FIX / "page_ppfas.html").read_text(), AMC_SOURCES["ppfas"].page)
    assert all(x.url.endswith(".xlsx") for x in pp)  # the consolidated old-format .xls is skipped
    mine = pick_links("ppfas", pp, "parag parikh flexicap fund", 2)
    assert [(x.label, x.month) for x in mine] == [("PPFCF", "2026-08"), ("PPFCF", "2026-07")]
    assert mine[0].url == ("https://amc.ppfas.com/downloads/portfolio-disclosure/2026/"
                           "PPFCF_PPFAS_Monthly_Portfolio_Report_August_31_2026.xlsx")  # fmt: skip
    nip = discover_links("nippon", (FIX / "page_nippon.html").read_text(), AMC_SOURCES["nippon"].page)
    assert [x.month for x in nip][:3] == ["2026-08", "2026-07", "2026-06"]  # 'July' spelled out is read too
    assert "FORTNIGHTLY" not in " ".join(x.url for x in nip)
    dsp = discover_links("dsp", (FIX / "page_dsp.html").read_text(), AMC_SOURCES["dsp"].page)
    assert dsp[0].month == "2026-08" and dsp[0].url.endswith("dsp-monthend-portfolio-as-on-31-aug-2026.zip")


def test_url_allowlist():
    assert check_url("https://www.axismf.com/1/5/x.xlsx")
    for bad in ("http://www.axismf.com/x.xlsx", "https://example.com/x.xlsx", "file:///etc/passwd",
                "https://www.axismf.com.evil.com/x.xlsx"):  # fmt: skip
        with pytest.raises(AmcPortfolioError):
            check_url(bad)


# ----------------------------------------------------------------------------------------------- store
def test_store_months_aliases_proxy_and_active_share(tmp_path):
    from finresearch.portfolio.lookthrough import (
        PortfolioStore,
        active_share_json,
        fund_detail,
        proxy_candidates,
    )

    st = PortfolioStore(tmp_path)
    sha_a, _ = st.add_file(fx("ppfas_ppfcf_2026-08.xlsx"), "a.xlsx")
    st.add_file(fx("ppfas_ppfcf_2026-07.xlsx"), "b.xlsx")
    assert st.add_file(fx("ppfas_ppfcf_2026-08.xlsx"), "a.xlsx")[0] == sha_a  # idempotent by content
    key = "parag parikh flexicap fund"
    assert [f.month for f in st.find(key)] == ["2026-08", "2026-07"]
    d = fund_detail(st, key, date(2026, 9, 30))
    assert d["file"]["month"] == "2026-08" and d["file"]["stale"] is False
    assert [x["month"] for x in d["style"]] == ["2026-07", "2026-08"] and d["style"][1]["drift_pct"] > 0
    assert fund_detail(st, key, date(2026, 11, 1))["file"]["stale"] is True  # > 45 days after month-end
    # Nifty 500: no index-fund proxy stored -> no number, a reason
    a = active_share_json(st, st.latest(key))
    assert a["active_share"] is None and "Nifty 500 TRI" in a["reason"]
    st.add_file(fx("nippon_2026-08.xlsx"), "n.xlsx")
    gf = st.latest("nippon india growth midcap fund")
    cands = [c["key"] for c in proxy_candidates(st, gf.portfolio.benchmark)]
    assert cands == ["nippon india etf nifty midcap 150"]  # not the Nifty 50 BeES
    got = active_share_json(st, gf)
    assert 0 < got["active_share"] < 100 and got["proxy"]["name"] == "Nippon India ETF Nifty Midcap 150"
    st.add_file(
        fx("axis_midcap_2026-08.xlsx"), "ax.xlsx"
    )  # benchmark BSE Midcap 150: a different index, no proxy
    assert proxy_candidates(st, "BSE MIDCAP 150 TRI") == []
    st.set_alias("999999", key)
    assert st.key_for("999999", "Some other name") == key
    assert st.delete_file(sha_a) and [f.month for f in st.find(key)] == ["2026-07"]


# ----------------------------------------------------------------------------------------------- API
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
TODAY = date(2026, 9, 30)


def fake_http():
    from finresearch.adapters.http import PoliteClient

    routes = {
        AMC_SOURCES["ppfas"].page: (FIX / "page_ppfas.html").read_bytes(),
        "https://amc.ppfas.com/downloads/portfolio-disclosure/2026/PPFCF_PPFAS_Monthly_Portfolio_Report_August_31_2026.xlsx":
            fx("ppfas_ppfcf_2026-08.xlsx"),
        "https://amc.ppfas.com/downloads/portfolio-disclosure/2026/PPFCF_PPFAS_Monthly_Portfolio_Report_July_31_2026.xlsx":
            fx("ppfas_ppfcf_2026-07.xlsx"),
        "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx":
            fx("amfi_cap_list_2025-12.xlsx"),
    }  # fmt: skip
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        seen.append(url)
        body = routes.get(url)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404, content=b"nope")

    async def no_sleep(_s: float) -> None:
        return None

    factory = lambda: PoliteClient(cache_dir=None, transport=httpx.MockTransport(handler), sleep=no_sleep)  # noqa: E731
    return factory, seen


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.adapters.amfi import SchemeNav
    from finresearch.adapters.nse import Quote
    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources
    from finresearch.db import session_scope

    navs = [SchemeNav("122639", "Parag Parikh Flexi Cap Fund", "Direct Plan", "Growth", "INF879O01027", None, D("88"),
                      TODAY, "Equity Scheme - Flexi Cap Fund", "PPFAS Mutual Fund"),
            SchemeNav("120505", "Axis Midcap Fund", "Direct Plan", "Growth Option", "INF846K01EH3", None, D("137"),
                      TODAY, "Equity Scheme - Mid Cap Fund", "Axis Mutual Fund"),
            SchemeNav("100001", "Example Liquid Fund", "Direct Plan", "Growth", None, None, D("10"), TODAY,
                      "Debt Scheme - Liquid Fund", "Example Mutual Fund")]  # fmt: skip

    async def nav_all():
        return navs

    async def quote(symbol):
        if symbol != "HDFCBANK":
            raise LookupError(symbol)
        return Quote(
            symbol="HDFCBANK",
            last_price=D(700),
            issued_shares=D(7_600_000_000),
            industry="Private Sector Bank",
        )

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                       "portfolio_snapshot, portfolio_setting"))  # fmt: skip
    app = create_app(nav_all=nav_all)
    app.state.markets = MarketSources(quote=quote, today=lambda: TODAY)
    factory, seen = fake_http()
    app.state.lookthrough_http = factory
    with TestClient(app) as c:
        c.seen = seen  # type: ignore[attr-defined]
        yield c


def buy(c, **kw):
    body = {"account": "Manual", "day": "2025-01-02", "kind": "buy", **kw}
    r = c.post("/api/portfolio/transactions", headers=ORIGIN, json=body)
    assert r.status_code == 201, r.text


def test_api_fetch_upload_lookthrough_and_overlap(client):
    c = client
    buy(
        c,
        asset_type="mf",
        name="Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
        scheme_code="122639",
        quantity="1000",
        price="50",
    )  # value 1000 x 88 = 88,000
    buy(
        c,
        asset_type="mf",
        name="Axis Midcap Fund - Direct Growth",
        scheme_code="120505",
        quantity="100",
        price="100",
    )  # 13,700
    buy(
        c,
        asset_type="stock",
        name="HDFC Bank Ltd",
        nse_symbol="HDFCBANK",
        isin="INE040A01034",
        quantity="10",
        price="600",
    )  # 7,000
    buy(
        c, asset_type="mf", name="Example Liquid Fund", scheme_code="100001", quantity="100", price="10"
    )  # 1,000, no file
    # before any file: every fund is "without a holdings file", and the numbers still reconcile
    lt0 = c.get("/api/lookthrough").json()
    assert lt0["reconciliation"] == 0 and lt0["total"] == 109700.0
    assert {b["kind"]: b["value"] for b in lt0["buckets"]}["no_file"] == 102700.0
    # PPFAS: discovered on the AMC page and downloaded (2 months for the drift)
    r = c.post("/api/lookthrough/fetch", headers=ORIGIN, json={"scheme_code": "122639", "months": 2})
    assert r.status_code == 200, r.text
    assert r.json()["months"] == ["2026-08", "2026-07"] and r.json()["errors"] == []
    # #214 coverage on the portfolio cards: Axis (13,700, paste/upload) + the liquid fund (1,000, no adapter) are not
    # looked through: 14,700 / 109,700 = 13.40 % of the portfolio; before the fetch it was 102,700 / 109,700 = 93.62 %
    assert lt0["coverage"]["not_looked_through_pct"] == 93.62
    cov = c.get("/api/portfolio").json()["lookthrough_coverage"]
    assert (cov["not_looked_through_pct"], cov["fund_pct"], cov["looked_through"], cov["funds_total"]) == (
        13.4, 93.62, 1, 3)  # fmt: skip
    assert cov["text"].startswith(
        "Direct stocks only — 13 % of the portfolio is in funds not looked through (2 of 3"
    )
    assert c.get("/api/lookthrough").json()["coverage"]["not_looked_through_pct"] == 13.4
    # Axis: no automatic discovery -> a pasted link off the allow-list is refused; the file is uploaded instead
    assert c.post("/api/lookthrough/fetch", headers=ORIGIN, json={"scheme_code": "120505"}).status_code == 422
    assert (
        c.post(
            "/api/lookthrough/fetch", headers=ORIGIN, json={"url": "https://evil.example/x.xlsx"}
        ).status_code
        == 422
    )
    up = c.post(
        "/api/lookthrough/upload",
        headers=ORIGIN,
        json={
            "filename": "axis.xlsx",
            "content_b64": base64.b64encode(fx("axis_midcap_2026-08.xlsx")).decode(),
        },
    )
    assert up.status_code == 200, up.text
    assert up.json()["schemes"][0]["codes"] == ["120505"]  # linked to the AMFI scheme by name
    bad = c.post(
        "/api/lookthrough/upload",
        headers=ORIGIN,
        json={"filename": "x.pdf", "content_b64": base64.b64encode(b"%PDF").decode()},
    )
    assert bad.status_code == 422
    lt = c.get("/api/lookthrough").json()
    assert lt["reconciliation"] == 0 and lt["total"] == 109700.0
    hdfc = next(s for s in lt["stocks"] if s["key"] == "INE040A01034")
    # direct 7,000 + 88,000 x 7.63 % from PPFCF = 13,714.40; Axis Midcap does not hold it in the trimmed file
    assert hdfc["value"] == pytest.approx(7000 + 88000 * 0.0763)
    assert {x["source"] for x in hdfc["routes"]} == {"Direct", "Parag Parikh Flexi Cap Fund"}
    assert hdfc["cap"] == "Large Cap" and lt["cap_list"]["as_of"] == "2025-12-31"
    buckets = {b["kind"]: b["value"] for b in lt["buckets"]}
    assert buckets["arbitrage"] > 0 and buckets["foreign_equity"] > 0 and buckets["no_file"] == 1000.0
    assert len(lt["overlap"]) == 1 and lt["overlap"][0]["common"] >= 1
    conc = lt["concentration"]  # the Concentration tab's hook: the same measures on the looked-through equity
    assert (
        conc["n_effective"] > 10
        and conc["largest"] == "HDFC Bank Ltd"
        and conc["fund_coverage_pct"] == pytest.approx(101700 / 102700 * 100, abs=0.01)
    )
    # fund page card: overlap with the other held funds, and with a chosen fund
    f = c.get("/api/lookthrough/funds/122639", params={"with": "120505"}).json()
    assert f["file"]["month"] == "2026-08" and f["auto_fetch"] is True and f["is_held"] is True
    assert f["with"]["available"] and f["with"]["overlap_pct"] == lt["overlap"][0]["overlap_pct"]
    assert {h["code"] for h in f["held"]} == {"120505", "100001"}
    assert f["active_share"]["active_share"] is None  # no Nifty 500 index-fund proxy stored
    assert "not a recommendation" in f["disclaimer"].lower()
    m = c.get("/api/lookthrough/overlap", params={"codes": "122639,120505,100001"}).json()
    assert len(m["pairs"]) == 1 and m["funds"][2]["file"]["available"] is False
    # manual link and file deletion
    assert (
        c.put(
            "/api/lookthrough/links", headers=ORIGIN, json={"scheme_code": "100001", "key": "nope"}
        ).status_code
        == 404
    )
    src = c.get("/api/lookthrough/sources").json()
    assert {s["key"] for s in src["sources"]} == {"ppfas", "nippon", "dsp", "axis", "quant", "tata"} and len(
        src["files"]
    ) == 3
    assert {u["key"] for u in src["unsupported"]} == {"hdfc", "sbi", "icici", "kotak", "mirae"}
    assert c.delete(f"/api/lookthrough/files/{src['files'][0]['sha']}", headers=ORIGIN).status_code == 200
    assert all(u.startswith("https://") for u in c.seen)


def test_cap_by_issuer_after_isin_change_and_reparse(tmp_path):
    import json

    from finresearch.portfolio.lookthrough import PortfolioStore, cap_lookup

    st = PortfolioStore(tmp_path)
    st.save_cap_list(
        {"as_of": "2025-12-31", "by_isin": {"INE237A01028": [16, "Large Cap"], "INE237A16AB1": [1, "X"]}}
    )
    cap_of, meta = cap_lookup(st)
    assert cap_of("INE237A01036") == "Large Cap"  # same issuer's equity after a split: new ISIN, same company
    assert cap_of("INE237A16ZZ9") is None  # not equity: no fallback
    assert meta["as_of"] == "2025-12-31"
    sha, _ = st.add_file(fx("ppfas_ppfcf_2026-08.xlsx"), "a.xlsx")
    (tmp_path / "parsed" / f"{sha}.json").write_text(json.dumps([]))  # an old-format cache
    fresh = PortfolioStore(tmp_path)
    assert len(fresh.parsed(sha)[0].holdings) == 83  # re-parsed from the kept original
    alphabet = next(h for h in fresh.parsed(sha)[0].holdings if h.isin == "US02079K3059")
    assert (
        alphabet.industry == "Computer Software: Programming, Data Processing"
    )  # '##' footnote marker dropped


def test_percent_text_totals_and_mixed_cells_keep_one_scale():
    # a GRAND TOTAL written as '100.00%' text is a percent total: the plain 55.5 must not become 5550
    [p] = parse_file(synthetic([["F"], ["Name", "ISIN", "% to NAV"], ["A", "INE000A01011", 55.5],
                                ["B", "INE000B01011", "40.00%"], ["Grand Total", None, "100.00%"]]), "p.xlsx")  # fmt: skip
    assert [h.weight for h in p.holdings] == [D("55.5"), D("40.0000")]
    # a fraction-scaled column with a '%' text cell: 0.555 -> 55.5 and '40.00%' -> 40
    [q] = parse_file(synthetic([["F"], ["Name", "ISIN", "% to NAV"], ["A", "INE000A01011", 0.555],
                                ["B", "INE000B01011", "40.00%"], ["Grand Total", None, 1]]), "q.xlsx")  # fmt: skip
    assert [h.weight for h in q.holdings] == [D("55.500"), D("40.0000")]
    # only '%' text and no total: each cell is read once as a percent
    [r] = parse_file(synthetic([["F"], ["Name", "ISIN", "% to NAV"], ["A", "INE000A01011", "60%"],
                                ["B", "INE000B01011", "30%"]]), "r.xlsx")  # fmt: skip
    assert [h.weight for h in r.holdings] == [D("60"), D("30")]


@respx.mock
async def test_download_rechecks_every_redirect_hop_against_the_allow_list(tmp_path):
    from finresearch.portfolio.lookthrough import PortfolioStore, fetch_url

    host = sorted(h for h in ALLOWED_HOSTS if "." in h)[0]
    ok = f"https://{host}/files/a.xlsx"
    respx.get(ok).mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1:8710/api/profile"})
    )
    store = PortfolioStore(tmp_path)
    with pytest.raises(AmcPortfolioError, match="only https links"):
        await fetch_url(store, ok)  # a redirect off the allow-list (here: this machine's own API) is refused
    good = f"https://{host}/files/b.xlsx"
    final = f"https://{host}/files/b-final.xlsx"
    respx.get(good).mock(return_value=httpx.Response(301, headers={"location": "/files/b-final.xlsx"}))
    respx.get(final).mock(return_value=httpx.Response(200, content=synthetic(
        [["F"], ["Name", "ISIN", "% to NAV"], ["A", "INE000A01011", 60], ["B", "INE000B01011", 40],
         ["Grand Total", None, 100]])))  # fmt: skip
    _key, parsed = await fetch_url(store, good)
    assert parsed and [h.weight for h in parsed[0].holdings] == [D("60"), D("40")]


def test_a_portfolio_zip_bomb_is_refused_before_inflating(monkeypatch):
    import io
    import zipfile

    import pytest

    from finresearch.adapters import amc_portfolio

    monkeypatch.setattr(amc_portfolio, "MAX_INFLATED_BYTES", 1_000_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", b"<x/>")
        z.writestr("xl/worksheets/sheet1.xml", b"\0" * 5_000_000)  # 5 MB of zeros in a ~5 KB file
    with pytest.raises(amc_portfolio.AmcPortfolioError, match="inflate"):
        parse_file(buf.getvalue(), "bomb.xlsx")

"""#214: quant and Tata monthly portfolios (synthetic workbooks in their layouts), their discovery, the supported /
unsupported fund-house registry, the coverage figure and the monthly fetch. Offline only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import respx
from amc_synthetic import quant_xlsx, tata_xlsx

from finresearch.adapters.amc_portfolio import (
    AMC_SOURCES,
    AMC_UNSUPPORTED,
    QUANT_LIST_URL,
    check_url,
    discover_links,
    house_for,
    parse_as_of,
    parse_file,
    pick_links,
    quant_list_body,
    source_for_amc,
)


def by_isin(p, isin):
    return next(h for h in p.holdings if h.isin == isin)


# ----------------------------------------------------------------------------------------------- parsing
def test_quant_layout_rating_before_industry_and_unhedged_futures():
    [p] = parse_file(quant_xlsx(), "quant_Example_Flexi_Cap_Fund_31_Aug_2026.xlsx")
    # the title, not the description line under it (the longest text) nor the house name
    assert (p.scheme_name, p.key, p.as_of, p.benchmark) == ("quant Example Flexi Cap Fund",
                                                             "quant example flexicap fund", date(2026, 8, 31),
                                                             "NIFTY 500 TRI")  # fmt: skip
    a = by_isin(p, "INE00QA01011")
    # RATING ('N.A.') sits before INDUSTRY: the industry column is read, and 'N.A.' is no industry
    assert (a.industry, a.weight, a.value_lakh, a.kind) == (
        "Auto Components",
        D("40.0"),
        D("400.0"),
        "equity",
    )
    assert by_isin(p, "IN002026X099").industry is None and by_isin(p, "IN002026X099").kind == "govt"
    assert by_isin(p, "INCBLO010926").kind == "debt"  # TREPS
    assert p.grand_total_lakh == D("1000.0")
    assert p.weight_of("equity") == D("70.0")
    # 25.3 % in long stock futures has no ISIN line: said, not silently dropped
    assert len(p.warnings) == 1 and "25.3 % of net assets in non-hedging derivative" in p.warnings[0]
    [q] = parse_file(quant_xlsx(futures_pct=None), "q.xlsx")
    assert q.warnings == []  # 'NIL': nothing to warn about


def test_tata_layout_second_header_totals_and_two_digit_year():
    ps = {p.sheet: p for p in parse_file(tata_xlsx(), "Monthly Portfolio as on 31st August 2026.xlsx")}
    assert set(ps) == {"TEXFLX", "TEXSML"}  # the Index and risk-o-meter sheets hold no portfolio
    p = ps["TEXFLX"]
    assert (p.scheme_name, p.key, p.as_of) == ("TATA EXAMPLE FLEXI CAP FUND", "tata example flexicap fund",
                                               date(2026, 8, 31))  # fmt: skip
    # the repeated header above the debt block used to end the table: the G-Sec, NET ASSETS = 100 were lost
    assert (
        by_isin(p, "IN0020240019").kind == "govt" and p.grand_total_lakh == D("1000.0") and p.warnings == []
    )
    assert (by_isin(p, "INE00TA01014").industry, by_isin(p, "INE00TA01014").weight) == ("Banks", D("55.0"))
    assert by_isin(p, "INE00TA01014").value_lakh == D("550.0")  # 'MKT VAL(Rs. Lacs)'
    assert by_isin(p, "INF00TC01AB3").kind == "mf_units"  # ETF units listed under equity are not a stock
    assert p.weight_of("equity") == D("85.0")
    assert parse_as_of("Portfolio as on 31-08-26") == date(2026, 8, 31)


# ----------------------------------------------------------------------------------------------- discovery
FIX = Path(__file__).parent / "fixtures" / "amc"
QUANT_FILE = "https://quantmutual.com/Admin/disclouser/quant_Example_Flexi_Cap_Fund_31_Aug_2026.xlsx"
TATA_AUG = "https://betacms.tatamutualfund.com/system/files/2026-09/Monthly%20Portfolio%20as%20on%2031st%20August%202026.xlsx"


def test_quant_and_tata_discovery():
    q = discover_links("quant", (FIX / "quant_list_2026-08.json").read_text(), AMC_SOURCES["quant"].page)
    assert [(x.label, x.month) for x in q] == [("quant example liquid fund", "2026-08"),
                                               ("quant example large and midcap fund", "2026-08"),
                                               ("quant example flexicap fund", "2026-08")]  # fmt: skip
    # one file per scheme: only the held scheme's file is picked, never the whole month
    assert [x.url for x in pick_links("quant", q, "quant example flexicap fund", 1)] == [QUANT_FILE]
    assert pick_links("quant", q, "quant example small cap fund", 1) == []
    assert quant_list_body(2026, 8) == b"{id:'8',cat:'MONTHLY PORTFOLIO - FUND - WISE',tab:'2026'}"
    t = discover_links("tata", (FIX / "page_tata.html").read_text(), AMC_SOURCES["tata"].page)
    # month-end portfolios only: the 15-Aug fortnightly file and the AAUM disclosure are not monthly portfolios
    assert [(x.month, x.url) for x in t] == [("2026-08", TATA_AUG), ("2026-07", TATA_AUG.replace("2026-09", "2026-08")
                                                                     .replace("August", "July"))]  # fmt: skip
    assert all(check_url(x.url) for x in t)


def test_house_matching_uses_word_boundaries_and_the_amc_heading_first():
    assert (
        house_for("quant Mutual Fund") == "quant"
        and house_for(None, "quant Small Cap Fund - Direct Growth") == "quant"
    )
    assert house_for("Quantum Mutual Fund", "Quantum Long Term Equity Value Fund") is None  # not quant
    assert (
        house_for("Tata Mutual Fund") == "tata"
        and house_for(None, "HDFC Flexi Cap Fund - Direct Plan") == "hdfc"
    )
    # the AMC heading wins over words in the scheme name
    assert house_for("Nippon India Mutual Fund", "Nippon India ETF Nifty 50 BeES") == "nippon"
    assert house_for("DSP Mutual Fund", "DSP Nifty HDFC Group ETF") == "dsp"
    assert source_for_amc("HDFC Mutual Fund") is None and "hdfc" in AMC_UNSUPPORTED
    assert source_for_amc("Tata Mutual Fund") == "tata" and source_for_amc(None, "PPFAS Flexi") == "ppfas"
    assert house_for("Example Asset Mutual Fund", "Example Bluechip Fund") is None


def fake_amc_client(routes: dict[str, bytes], posts: dict[bytes, bytes] | None = None):
    """A PoliteClient whose transport answers from `routes` (GET by URL) and `posts` (quant's list, by body)."""
    import httpx

    from finresearch.adapters.http import PoliteClient

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        seen.append(f"{request.method} {url}")
        if request.method == "POST":
            body = (posts or {}).get(request.content)
            return httpx.Response(200, content=body if body is not None else b'{"d":"<ul></ul>"}')
        body = routes.get(url)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404, content=b"nope")

    async def no_sleep(_s: float) -> None:
        return None

    return PoliteClient(cache_dir=None, transport=httpx.MockTransport(handler), sleep=no_sleep), seen


async def test_fetch_scheme_quant_posts_one_month_and_downloads_one_file(tmp_path):
    from finresearch.portfolio.lookthrough import PortfolioStore, fetch_scheme

    store = PortfolioStore(tmp_path)
    http, seen = fake_amc_client({QUANT_FILE: quant_xlsx()},
                                 {quant_list_body(2026, 8): (FIX / "quant_list_2026-08.json").read_bytes()})  # fmt: skip
    res = await fetch_scheme(store, "quant example flexicap fund", "quant Mutual Fund", client=http,
                             today=date(2026, 10, 6))  # fmt: skip
    await http.aclose()
    # September's list is empty (not published by 06-Oct), August's has the scheme: two POSTs, one download
    assert seen == [f"POST {QUANT_LIST_URL}", f"POST {QUANT_LIST_URL}", f"GET {QUANT_FILE}"]
    assert res["months"] == ["2026-08"] and res["matched"] and res["errors"] == []


async def test_fetch_scheme_tata_reads_the_house_workbook(tmp_path):
    from finresearch.portfolio.lookthrough import PortfolioStore, fetch_scheme

    store = PortfolioStore(tmp_path)
    page = AMC_SOURCES["tata"].page
    http, seen = fake_amc_client({page: (FIX / "page_tata.html").read_bytes(), TATA_AUG: tata_xlsx()})
    res = await fetch_scheme(store, "tata example smallcap fund", "Tata Mutual Fund", client=http)
    await http.aclose()
    assert seen == [f"GET {page}", f"GET {TATA_AUG}"] and res["months"] == ["2026-08"]
    assert store.latest("tata example flexicap fund") is not None  # one workbook brings every Tata scheme


@respx.mock
async def test_tata_redirect_to_www_passes_the_hop_check(tmp_path):
    import httpx

    from finresearch.portfolio.lookthrough import PortfolioStore, fetch_url

    www = TATA_AUG.replace("betacms.", "www.")
    respx.get(TATA_AUG).mock(return_value=httpx.Response(302, headers={"location": www}))
    respx.get(www).mock(return_value=httpx.Response(200, content=tata_xlsx()))
    _sha, ps = await fetch_url(PortfolioStore(tmp_path), TATA_AUG)
    assert {p.sheet for p in ps} == {"TEXFLX", "TEXSML"}


# ----------------------------------------------------------------------------------------------- coverage
def test_coverage_share_of_portfolio_in_funds_not_looked_through(tmp_path):
    from finresearch.portfolio.lookthrough import FundValue, PortfolioStore, coverage

    store = PortfolioStore(tmp_path)
    store.add_file(quant_xlsx(), "quant_Example_Flexi_Cap_Fund_31_Aug_2026.xlsx")
    today = date(2026, 10, 6)
    funds = [FundValue("1", "quant Example Flexi Cap Fund - Direct Plan - Growth", D(100), "quant Mutual Fund"),
             FundValue("2", "Tata Example Small Cap Fund - Direct Plan - Growth", D(200), "Tata Mutual Fund"),
             FundValue("3", "HDFC Example Flexi Cap Fund - Direct Plan", D(150), "HDFC Mutual Fund"),
             FundValue("4", "Example House Bluechip Fund - Direct", D(20), None)]  # fmt: skip
    c = coverage(D(1000), funds, store, today)  # 530 in direct stocks
    # by hand: funds 100 + 200 + 150 + 20 = 470 of 1000 = 47 %; not looked through 200 + 150 + 20 = 370 = 37 %
    assert (c["fund_pct"], c["not_looked_through_pct"]) == (47.0, 37.0)
    assert (c["funds_total"], c["looked_through"], c["not_fetched"], c["unsupported"]) == (4, 1, 1, 2)
    assert c["text"] == ("Direct stocks only — 37 % of the portfolio is in funds not looked through (3 of 4 funds: "
                         "2 unsupported, 1 not fetched)")  # fmt: skip
    st = {f["code"]: f for f in c["funds"]}
    assert st["1"]["status"] == "looked_through" and st["1"]["month"] == "2026-08"
    assert "Akamai" in st["3"]["reason"] and "no adapter" in st["4"]["reason"]
    # a fund without a price: the share is unknown, never 0 (and never "all looked through")
    u = coverage(
        D(880), [*funds[:3], FundValue("4", "Example House Bluechip Fund", None, None)], store, today
    )
    assert u["not_looked_through_pct"] is None and u["unpriced"] == 1 and "unknown" in u["text"]
    # every fund looked through: 0 % blind, said as such
    a = coverage(D(630), funds[:1], store, today)
    assert a["not_looked_through_pct"] == 0.0 and "all 1 are looked through" in a["text"]
    assert coverage(D(500), [], store, today)["text"] is None  # no funds: nothing to say


# ----------------------------------------------------------------------------------------------- monthly fetch
def test_monthly_fetch_window_after_sebis_ten_days():
    from datetime import UTC, datetime

    from finresearch.monitor.lookthrough_fetch import due_slot, target_month

    ist = lambda d, h: datetime(2026, 10, d, h - 6, 0, tzinfo=UTC)  # noqa: E731  (IST = UTC + 5:30; h:30 IST)
    assert due_slot(ist(10, 12)) is None  # day 10: SEBI's deadline day, files may still be coming
    assert due_slot(ist(11, 7)) is None  # 07:30 IST, before RUN_AFTER
    assert due_slot(ist(11, 9)) == "lookthrough:2026-10-11"
    assert due_slot(ist(25, 9)) == "lookthrough:2026-10-25" and due_slot(ist(26, 9)) is None
    assert (target_month(date(2026, 10, 11)), target_month(date(2026, 1, 11))) == ("2026-09", "2025-12")


HELD = [("1", "quant Example Flexi Cap Fund - Direct Plan - Growth"),
        ("2", "Tata Example Small Cap Fund - Direct Plan - Growth"),
        ("3", "HDFC Example Flexi Cap Fund - Direct Plan - Growth"),
        ("4", "Axis Example Midcap Fund - Direct Growth"),
        ("5", "Tata Example Flexi Cap Fund - Regular Plan - Growth"),
        ("6", "Example House Bluechip Fund - Direct")]  # fmt: skip


async def test_monthly_fetch_reasons_per_fund_and_once_a_day(env, tmp_path):
    from datetime import UTC, datetime

    from finresearch.monitor.lookthrough_fetch import lookthrough_step
    from finresearch.portfolio.lookthrough import PortfolioStore

    store = PortfolioStore(tmp_path / "lt")
    routes = {AMC_SOURCES["tata"].page: (FIX / "page_tata.html").read_bytes(), TATA_AUG: tata_xlsx(),
              QUANT_FILE: quant_xlsx()}  # fmt: skip
    posts = {quant_list_body(2026, 8): (FIX / "quant_list_2026-08.json").read_bytes()}
    # 12-Sep-2026 09:30 IST: the August portfolios are due
    http, seen = fake_amc_client(routes, posts)
    now = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
    res = await lookthrough_step(now, held=HELD, store=store, client=http)
    assert res["lookthrough"] == {"fetched": 2, "up_to_date": 1, "unsupported": 2, "manual": 1}
    # quant: one list request + its own file; Tata: one page + one workbook for both Tata funds
    assert seen == [
        f"POST {QUANT_LIST_URL}",
        f"GET {QUANT_FILE}",
        f"GET {AMC_SOURCES['tata'].page}",
        f"GET {TATA_AUG}",
    ]
    assert await lookthrough_step(now, held=HELD, store=store, client=http) == {}  # claimed: once a day
    # 12-Oct-2026: September is due; neither house has published it (quant's September list is empty)
    seen.clear()
    later = await lookthrough_step(
        datetime(2026, 10, 12, 4, 0, tzinfo=UTC), held=HELD, store=store, client=http
    )
    assert later["lookthrough"] == {"not_published": 3, "unsupported": 2, "manual": 1}
    # quant: September's list (empty) and August's; Tata: its page once for both funds
    assert seen == [f"POST {QUANT_LIST_URL}", f"POST {QUANT_LIST_URL}", f"GET {AMC_SOURCES['tata'].page}"]
    await http.aclose()
    assert (
        f"GET {QUANT_FILE}" not in seen and f"GET {TATA_AUG}" not in seen
    )  # stored files are not re-downloaded


async def test_monthly_fetch_reason_lines():
    import tempfile
    from pathlib import Path as P

    from finresearch.monitor.lookthrough_fetch import fetch_held
    from finresearch.portfolio.lookthrough import PortfolioStore

    http, _ = fake_amc_client({}, {})
    with tempfile.TemporaryDirectory() as d:
        lines = {x["code"]: x for x in await fetch_held(date(2026, 10, 12), HELD[:4] + HELD[5:],
                                                         store=PortfolioStore(P(d)), client=http)}  # fmt: skip
    await http.aclose()
    assert (
        lines["1"]["status"] == "not_published"
        and "has not published the 2026-09 portfolio" in lines["1"]["reason"]
    )
    assert lines["2"]["status"] == "error" and "HTTP 404" in lines["2"]["reason"]  # the page itself failed
    assert lines["3"]["status"] == "unsupported" and "Akamai" in lines["3"]["reason"]
    assert lines["4"]["status"] == "manual" and "paste the file link" in lines["4"]["reason"]
    assert lines["6"]["status"] == "unsupported" and "no adapter" in lines["6"]["reason"]


async def test_monthly_fetch_says_when_the_house_file_lacks_the_scheme(tmp_path):
    from finresearch.monitor.lookthrough_fetch import fetch_held
    from finresearch.portfolio.lookthrough import PortfolioStore

    http, _ = fake_amc_client(
        {AMC_SOURCES["tata"].page: (FIX / "page_tata.html").read_bytes(), TATA_AUG: tata_xlsx()}
    )
    [line] = await fetch_held(date(2026, 9, 12), [("7", "Tata Example Mid Cap Fund - Direct")],
                              store=PortfolioStore(tmp_path), client=http)  # fmt: skip
    await http.aclose()
    # the August workbook arrived, but has no such scheme: not "not published", which would be retried forever
    assert line["status"] == "not_matched" and "link it on the Look-through page" in line["reason"]


def test_monitor_runs_the_fetch_and_shows_it_in_the_schedule(env, monkeypatch):
    from finresearch.monitor import jobs, scheduler

    assert jobs.Deps(None, None).lookthrough is False  # tests opt in; Deps.live() turns it on
    assert scheduler.schedule_json()["lookthrough"] == {
        "from_day": 11,
        "to_day": 25,
        "after": "08:00",
        "sebi_days": 10,
    }

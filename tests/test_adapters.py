"""Offline tests for the NSE / SEBI adapters and the polite HTTP client (respx mocks + recorded fixtures)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.http import IST, FetchRecord, PoliteClient
from finresearch.adapters.nse import (
    NSE_BASE,
    WARMUP_URL,
    NseClient,
    NseError,
    parse_ipo_detail,
    parse_nse_timestamp,
    parse_num,
    parse_price_band,
    rebase_times,
)
from finresearch.adapters.sebi import (
    AJAX_LISTING_URL,
    LISTING_URL,
    SebiClient,
    parse_detail_pdfs,
    parse_listing,
    parse_total_records,
)

FIX = Path(__file__).parent / "fixtures"
NSE_FIX = FIX / "nse"
SEBI_FIX = FIX / "sebi"
ORIENT = "ipo_detail_ORIENTCABL_20260928_1443.json"
MONEYVIEW = "ipo_detail_MONEYVIEW_20260928_1357.json"
ORIENT_RHP = (
    "https://www.sebi.gov.in/filings/public-issues/sep-2026/orient-cables-india-limited-rhp_104663.html"
)
MONEYVIEW_RHP = "https://www.sebi.gov.in/filings/public-issues/sep-2026/moneyview-limited-rhp_104597.html"


def load(name: str) -> object:
    return json.loads((NSE_FIX / name).read_text())


class FakeTime:
    """Deterministic clock + sleep so rate-limit/backoff tests are instant."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def make_client(tmp_path: Path, t: FakeTime | None = None, **kw) -> PoliteClient:
    t = t or FakeTime()
    kw.setdefault("cache_dir", tmp_path / "cache")
    return PoliteClient(sleep=t.sleep, clock=t.clock, backoff_base=0.0, **kw)


# --------------------------------------------------------------------------- parsing helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.32524175E8", Decimal("232524175")),
        ("1.176E7", Decimal("11760000")),
        ("3779440.0", Decimal("3779440")),
        ("6,62,77,860", Decimal("66277860")),
        ("    99", Decimal("99")),
        ("", None),
        ("-", None),
        (None, None),
    ],
)
def test_parse_num(raw, expected):
    assert parse_num(raw) == expected


def test_parse_timestamps_to_ist():
    a = parse_nse_timestamp("Updated as on 28-Sep-2026 13:54:00")
    b = parse_nse_timestamp("As on 28-Sep-2026 14:40:06 IST")
    c = parse_nse_timestamp("28-Sep-2026 13:51:00")
    assert a == datetime(2026, 9, 28, 13, 54, tzinfo=IST)
    assert b == datetime(2026, 9, 28, 14, 40, 6, tzinfo=IST)
    assert c is not None and c.utcoffset() == timedelta(hours=5, minutes=30)
    assert parse_nse_timestamp("") is None


def test_price_band_and_rebase():
    assert parse_price_band("Rs.159 to Rs.167") == (Decimal(159), Decimal(167))
    assert parse_price_band("Rs. 258 to Rs. 272 per Equity Share") == (Decimal(258), Decimal(272))
    # 100x at the low band on 1000 shares -> 125x when only 800 shares are on offer at the upper band
    assert rebase_times(Decimal(100), Decimal(1000), Decimal(800)) == Decimal(125)


# --------------------------------------------------------------------------- ipo-detail


def test_ipo_detail_combined_vs_nse_only():
    d = parse_ipo_detail("ORIENTCABL", load(ORIENT))
    assert d.company_name == "Orient Cables (India) Limited"
    assert d.combined is not None and d.nse_only is not None
    assert d.combined.source == "nse_combined" and d.nse_only.source == "nse_only"
    # combined (NSE+BSE) book is bigger than the NSE-only book
    assert d.combined.total_shares_bid == Decimal("98188530")
    assert d.nse_only.total_shares_bid == Decimal("66277860")
    assert d.combined.total_times == Decimal("6.556066963291017")
    assert d.nse_only.total_times == Decimal("4.425385412569342")
    assert d.combined.total_shares_offered == Decimal("14976743")
    # header row in activeCat is dropped; Total row kept with code None
    assert all(c.code != "Sr.No." for c in d.combined.categories)
    assert d.combined.categories[-1].is_total and d.combined.categories[-1].code is None
    assert [c.code for c in d.combined.top_level] == ["1", "2", "3"]
    rii = d.combined.category("3")
    assert rii is not None and rii.shares_offered == Decimal("7488372") and rii.times is not None
    sub = d.combined.category("1(a)")
    assert sub is not None and sub.shares_offered is None and sub.shares_bid == Decimal("3685")
    # timestamps: combined from activeCat.updateTime, NSE-only from the NSE demand graph
    assert d.combined.as_of == datetime(2026, 9, 28, 14, 39, tzinfo=IST)
    assert d.nse_only.as_of == datetime(2026, 9, 28, 14, 40, 6, tzinfo=IST)


def test_ipo_detail_demand_graph_and_issue_info():
    d = parse_ipo_detail("MONEYVIEW", load(MONEYVIEW))
    assert d.combined is not None
    assert d.combined.as_of == datetime(2026, 9, 28, 13, 54, tzinfo=IST)
    assert d.combined.total_shares_offered == Decimal("232524175")  # "2.32524175E8"
    assert d.combined.total_shares_bid == Decimal("7477941303")  # "7.477941303E9"
    g = d.demand_combined
    assert g is not None and g.scope == "combined"
    assert g.as_of == datetime(2026, 9, 28, 13, 51, tzinfo=IST)
    assert g.times_subscribed == Decimal("32.00")
    assert g.cutoff is not None and g.cutoff.cumulative_shares == Decimal("1425875157")
    assert {p.price for p in g.points} >= {"32", "33", "34"}
    assert d.demand_nse is not None and d.demand_nse.total_bids == Decimal("5687084403")
    info = d.issue_info
    assert info["Issue Period"] == "24-Sep-2026 to 28-Sep-2026"
    assert info["Bid Lot"].startswith("441 Equity Shares")
    assert info["Cut-off time for UPI Mandate Confirmation"].startswith("28-Sep-2026")
    assert not info["Issue Size"].startswith('"')  # literal wrapping quotes stripped
    assert d.price_band == (Decimal(32), Decimal(34))


def test_ipo_detail_tolerates_empty_payload():
    d = parse_ipo_detail("GONE", {"companyName": "GONE", "metaInfo": {}})
    assert d.combined is None and d.nse_only is None and d.issue_info == {}


# --------------------------------------------------------------------------- NseClient over respx


@respx.mock
async def test_nse_lists_parse(tmp_path):
    respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html>ok</html>"))
    respx.get(f"{NSE_BASE}/api/ipo-current-issue").mock(
        return_value=httpx.Response(200, json=load("ipo_current_issue.json"))
    )
    up = respx.get(f"{NSE_BASE}/api/all-upcoming-issues", params={"category": "ipo"}).mock(
        return_value=httpx.Response(200, json=load("all_upcoming_issues_ipo.json"))
    )
    respx.get(f"{NSE_BASE}/api/public-past-issues").mock(
        return_value=httpx.Response(200, json=load("public_past_issues_trimmed.json"))
    )
    async with NseClient(make_client(tmp_path)) as nse:
        cur = await nse.current_issues()
        upcoming = await nse.upcoming_issues()
        past = await nse.past_issues()
    orient = next(i for i in cur if i.symbol == "ORIENTCABL")
    assert orient.price_low == Decimal(258) and orient.price_high == Decimal(272)
    assert orient.shares_offered == Decimal("14976743") and orient.times_subscribed is not None
    assert up.called and all(i.times_subscribed is None for i in upcoming)
    sona = next(p for p in past if p.symbol == "SONA")
    assert sona.issue_price == Decimal(99) and sona.listing_date is not None
    corein = next(p for p in past if p.symbol == "COREIN")
    assert (
        corein.company.startswith("Coreintegra")
        and corein.issue_price is None
        and corein.listing_date is None
    )


@respx.mock
async def test_nse_rewarms_once_on_403(tmp_path):
    warm = respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    detail = respx.get(f"{NSE_BASE}/api/ipo-detail").mock(
        side_effect=[httpx.Response(403, text="denied"), httpx.Response(200, json=load(ORIENT))]
    )
    records: list[FetchRecord] = []
    async with NseClient(make_client(tmp_path, on_record=lambda f: records.append(f.record))) as nse:
        d = await nse.ipo_detail("ORIENTCABL")
    assert warm.call_count == 2 and detail.call_count == 2
    assert d.combined is not None and d.fetch is not None and d.fetch.status == 200
    assert d.fetch.params == {"symbol": "ORIENTCABL", "series": "EQ"}
    assert [r.status for r in records] == [200, 403, 200, 200]


@respx.mock
async def test_nse_gives_up_after_second_403(tmp_path):
    warm = respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    respx.get(f"{NSE_BASE}/api/ipo-current-issue").mock(return_value=httpx.Response(403))
    async with NseClient(make_client(tmp_path)) as nse:
        with pytest.raises(NseError):
            await nse.current_issues()
    assert warm.call_count == 2


# --------------------------------------------------------------------------- PoliteClient


@respx.mock
async def test_retry_on_503_then_success(tmp_path):
    route = respx.get("https://example.com/x").mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, text="ok")]
    )
    async with make_client(tmp_path) as c:
        r = await c.get("https://example.com/x")
    assert r.ok and r.text == "ok" and route.call_count == 3


@respx.mock
async def test_retry_on_timeout_then_raises(tmp_path):
    route = respx.get("https://example.com/t").mock(side_effect=httpx.ReadTimeout("slow"))
    async with make_client(tmp_path, max_retries=2) as c:
        with pytest.raises(httpx.ReadTimeout):
            await c.get("https://example.com/t")
    assert route.call_count == 3


@respx.mock
async def test_403_is_not_retried(tmp_path):
    route = respx.get("https://example.com/f").mock(return_value=httpx.Response(403))
    async with make_client(tmp_path) as c:
        r = await c.get("https://example.com/f")
    assert r.status == 403 and route.call_count == 1


@respx.mock
async def test_rate_limit_spaces_requests_per_host(tmp_path):
    respx.get(url__startswith="https://").mock(return_value=httpx.Response(200, text="x"))
    t = FakeTime()
    async with make_client(tmp_path, t) as c:
        for _ in range(3):
            await c.get(f"{NSE_BASE}/api/a")  # 2 req/s -> 0.5 s spacing
        await c.get("https://www.sebi.gov.in/a")  # different host: no wait
        await c.get("https://www.sebi.gov.in/b")  # 1 req/s -> 1.0 s spacing
    assert t.sleeps == pytest.approx([0.5, 0.5, 1.0])


@respx.mock
async def test_cache_hit_avoids_second_request(tmp_path):
    route = respx.get("https://example.com/c").mock(return_value=httpx.Response(200, json={"a": 1}))
    records: list[FetchRecord] = []
    async with make_client(tmp_path, on_record=lambda f: records.append(f.record)) as c:
        first = await c.get("https://example.com/c", params={"q": "1"}, cache_ttl=3600)
        second = await c.get("https://example.com/c", params={"q": "1"}, cache_ttl=3600)
        uncached = await c.get("https://example.com/c", params={"q": "1"})  # opt-in only
    assert route.call_count == 2
    assert not first.record.from_cache and second.record.from_cache
    assert second.json() == {"a": 1} and second.record.fetched_at == first.record.fetched_at
    assert second.record.sha256 == first.record.sha256 and not uncached.record.from_cache
    assert first.record.fetched_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert len(records) == 2  # recorder sees real fetches only


@respx.mock
async def test_cache_expires_after_ttl(tmp_path):
    route = respx.get("https://example.com/e").mock(return_value=httpx.Response(200, text="v"))
    clock = [datetime(2026, 9, 28, 10, 0, tzinfo=IST)]
    async with make_client(tmp_path, wall_clock=lambda: clock[0]) as c:
        await c.get("https://example.com/e", cache_ttl=60)
        clock[0] += timedelta(seconds=61)
        r = await c.get("https://example.com/e", cache_ttl=60)
    assert route.call_count == 2 and not r.record.from_cache


# --------------------------------------------------------------------------- SEBI


def test_sebi_listing_page1_and_ajax_page():
    page1 = (SEBI_FIX / "listing_rhp_page1.html").read_text()
    rows = parse_listing(page1, "rhp")
    assert len(rows) == 25 and parse_total_records(page1) == 1282
    first = rows[0]
    assert first.company == "Runwal Enterprises Limited" and first.doc_kind == "RHP"
    assert first.filing_id == 104683 and first.date.isoformat() == "2026-09-24"
    assert first.abridged_pdf_url and "commondocs" in first.abridged_pdf_url
    assert any(r.company == "Orient Cables (India) Limited" for r in rows)
    ajax = parse_listing((SEBI_FIX / "ajax_rhp_page2.html").read_text(), "rhp")  # single-quoted hrefs
    assert len(ajax) == 25 and ajax[0].company == "Steamhouse India Limited"
    drhp = parse_listing((SEBI_FIX / "listing_drhp_page1.html").read_text(), "drhp")
    assert drhp[0].title == "JSW One Platforms Limited - DRHP"  # double space normalised
    assert " " not in (drhp[0].abridged_pdf_url or "")


def test_sebi_detail_pdf_resolution():
    orient = parse_detail_pdfs((SEBI_FIX / "detail_orient_cables_rhp_104663.html").read_text(), ORIENT_RHP)
    assert orient[0] == "https://www.sebi.gov.in/sebi_data/attachdocs/sep-2026/1790144515401.pdf"
    assert any("commondocs" in u and "Abridged" in u for u in orient[1:])
    mv = parse_detail_pdfs((SEBI_FIX / "detail_moneyview_rhp_104597.html").read_text(), MONEYVIEW_RHP)
    assert mv[0] == "https://www.sebi.gov.in/sebi_data/attachdocs/sep-2026/1789975400167.pdf"


@respx.mock
async def test_sebi_client_pages_and_resolve(tmp_path):
    page1 = respx.get(LISTING_URL, params={"smid": "11"}).mock(
        return_value=httpx.Response(200, text=(SEBI_FIX / "listing_rhp_page1.html").read_text())
    )
    ajax = respx.post(AJAX_LISTING_URL).mock(
        return_value=httpx.Response(200, text=(SEBI_FIX / "ajax_rhp_page2.html").read_text())
    )
    respx.get(ORIENT_RHP).mock(
        return_value=httpx.Response(200, text=(SEBI_FIX / "detail_orient_cables_rhp_104663.html").read_text())
    )
    async with SebiClient(make_client(tmp_path)) as sebi:
        filings = await sebi.list_public_issue_filings("rhp", pages=2)
        pdfs = await sebi.resolve_pdf_url(ORIENT_RHP)
    assert page1.call_count == 1 and ajax.call_count == 1
    form = dict(httpx.QueryParams(ajax.calls.last.request.content.decode()))
    assert form["doDirect"] == "1" and form["smid"] == "11" and form["next"] == "n"
    assert len(filings) == 50 and all(f.listing == "rhp" for f in filings)
    assert pdfs[0].endswith("/attachdocs/sep-2026/1790144515401.pdf")

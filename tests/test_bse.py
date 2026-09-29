"""BSE SME IPOs: recorded api.bseindia.com payloads (29-Sep-2026), the radar merge, issue facts and the monitor."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.adapters.bse import (
    BseIssue,
    BseIssueDetail,
    OfferDocument,
    as_ipo_detail,
    name_key,
    parse_scrip_header,
    parse_smart_search,
    parse_sme_demand,
)
from finresearch.adapters.http import IST

FIX = Path(__file__).parent / "fixtures" / "bse"


def load(name: str):
    return json.loads((FIX / name).read_text())


def shivchem() -> BseIssueDetail:
    return BseIssueDetail.parse(load("issue_detail_8008_SHIVCHEM_20260929_1034.json"))


def shivchem_demand():
    return parse_sme_demand("SHIVCHEM", load("sme_category_demand_8008_20260929_1035.json"))


# --------------------------------------------------------------------------- parsing
def test_issue_list_keeps_only_sme_ipos():
    rows = [BseIssue.parse(r) for r in load("public_issues_20260929.json")["Table"]]
    sme = [i for i in rows if i.is_sme_ipo]
    # the recorded list also has mainboard, debt, rights and an SME FPO; only the SME IPOs remain
    assert {i.ipo_no for i in sme} == {8008, 8023, 8007}
    s = next(i for i in sme if i.ipo_no == 8008)
    assert (s.company, s.status, s.issue_start, s.issue_end) == (
        "Shivchem Agro Limited", "L", date(2026, 9, 28), date(2026, 9, 30)
    )  # fmt: skip
    assert (s.price_low, s.price_high) == (Decimal("59.00"), Decimal("62.00"))
    fpo = next(i for i in rows if i.ipo_no == 8006)
    assert fpo.price_low == fpo.price_high == Decimal("29.00") and not fpo.is_sme_ipo


def test_issue_details_give_the_lot_and_the_two_lot_minimum():
    d = shivchem()
    assert (d.symbol, d.scrip_code, d.market_lot, d.minimum_bid, d.min_lots) == (
        "SHIVCHEM",
        "4858",
        2000,
        4000,
        2,
    )
    assert (d.issue_open, d.issue_close, d.issue_size_shares) == (
        date(2026, 9, 28),
        date(2026, 9, 30),
        2260000,
    )
    assert d.lead_manager == "Shannon Advisors Private Limited" and d.registrar.startswith("Maashitla")
    assert d.as_of == datetime(2026, 9, 29, 10, 34, 21, tzinfo=IST)
    assert set(d.documents) == {"Red Herring Prospectus", "Price Band Advertisement"}
    info = d.issue_info()
    assert info["Issue Period"] == "28-Sep-2026 to 30-Sep-2026" and info["Price Range"] == "Rs.59 to Rs.62"
    assert info["Lot Size"] == "2000 Equity Shares" and info["Minimum Bid"] == "4000 Equity Shares (2 lots)"


def test_sme_category_demand_maps_to_qib_nii_rii_and_total():
    from finresearch.suggest.rules import is_sme, subscription_metrics

    snap = shivchem_demand()
    assert snap.as_of == datetime(2026, 9, 29, 10, 35, 43, tzinfo=IST) and snap.source == "bse_sme"
    assert (snap.total_times, snap.total_shares_offered, snap.total_shares_bid) == (
        Decimal("0.1301"), Decimal(2260000), Decimal(294000)
    )  # fmt: skip
    assert [c.code for c in snap.top_level] == ["1", "2", "3", "4", "5", "6"]
    detail = as_ipo_detail(shivchem(), snap)
    m = {k: v.value for k, v in subscription_metrics(detail).items()}
    assert m == {"total_times": Decimal("0.1301"), "qib_times": Decimal("0.0000"), "nii_times": Decimal("0.1294"),
                 "rii_times": Decimal("0.1445")}  # fmt: skip
    assert subscription_metrics(detail)["rii_times"].source == "BSE SME subscription"
    assert is_sme(detail.issue_info) and is_sme({}, detail)


def test_demand_before_the_issue_opens_has_no_timestamp_and_no_times():
    snap = parse_sme_demand("TNA", load("sme_category_demand_8023_not_open.json"))
    assert snap.as_of is None and snap.total_times is None


def test_offer_documents_resolve_to_bse_download_links():
    from finresearch.ingest.discover import bse_candidates
    from finresearch.ingest.documents import DocKind

    docs = [OfferDocument.parse(r) for r in load("offer_documents_trimmed.json")["table"]]
    armee = next(d for d in docs if d.company == "Armee Infotech Limited")
    assert (
        armee.rhp
        == "https://www.bseindia.com/corporates/download/343571/IPO Open/RHPFinal_20260918193821.pdf"
    )
    got = {(c.kind, c.title) for c in bse_candidates("Armee Infotech Ltd", None, docs)}
    assert got == {(DocKind.RHP, "BSE RHP"), (DocKind.DRHP, "BSE DRHP")}
    own = {c.kind: c.url for c in bse_candidates("Shivchem Agro Limited", shivchem(), docs)}
    assert own[DocKind.RHP].endswith("Prospectus_And_GID_Document_Prospectus_and_GID.zip")
    assert DocKind.PRICE_BAND_AD in own and all(
        c.source == "bse" for c in bse_candidates("x", shivchem(), docs)
    )


def test_baseline_claims_from_bse_issue_details():
    from finresearch.verify.baseline import parse_baseline

    got = {b.metric: str(b.value) for b in parse_baseline(shivchem().issue_info())}
    assert got == {"price_band_upper": "62", "price_band_lower": "59", "lot_size": "2000", "face_value": "5"}


def test_same_company_on_both_exchanges_is_matched_by_normalised_name():
    assert name_key("Shivchem Agro Ltd.") == name_key("SHIVCHEM AGRO LIMITED")
    assert name_key("Acme India Industries Limited") != name_key("Acme Universal Safezone9 Limited")


# --------------------------------------------------------------------------- client
async def test_client_sends_browser_headers_and_uses_the_sme_table():
    import httpx

    from finresearch.adapters.bse import BseClient
    from finresearch.adapters.http import PoliteClient

    seen = []
    bodies = {"BSEDemSchd": "demand_schedule_8008.json", "bbnew": "sme_category_demand_8008_20260929_1035.json",
              "GetMkt_ISSUE": "issue_detail_8008_SHIVCHEM_20260929_1034.json"}  # fmt: skip

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        name = next(v for k, v in bodies.items() if k in req.url.path)
        return httpx.Response(200, json=load(name))

    async def no_sleep(_):
        return None

    http = PoliteClient(transport=httpx.MockTransport(handler), cache_dir=None, sleep=no_sleep)
    async with BseClient(http) as bse:
        d = await bse.ipo_detail(8008)
    await http.aclose()
    assert d.symbol == "SHIVCHEM" and d.series == "SME" and d.combined.total_times == Decimal("0.1301")
    h = seen[0].headers
    assert h["origin"] == "https://www.bseindia.com" and h["sec-fetch-site"] == "same-site"
    assert h["accept"].startswith("application/json") and "Chrome" in h["user-agent"]
    assert [r.url.params["IPO_NO"] for r in seen] == ["8008"] * 3


async def test_client_rejects_a_block_page():
    import httpx

    from finresearch.adapters.bse import BseClient, BseError
    from finresearch.adapters.http import PoliteClient

    http = PoliteClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="<HTML>Access Denied")),
                        cache_dir=None)  # fmt: skip
    with pytest.raises(BseError, match="403"):
        async with BseClient(http) as bse:
            await bse.public_issues()
    await http.aclose()


# --------------------------------------------------------------------------- pipeline facts
async def test_bse_only_company_gets_its_issue_facts_and_baseline_from_bse(env, tmp_path, monkeypatch):
    from sqlalchemy import select

    from finresearch.adapters import bse
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator.ipo import IpoPipeline, create_run

    class FakeBse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def issue_detail(self, ipo_no):
            assert ipo_no == 8008
            return shivchem()

    slug = "shivchem-" + tmp_path.name[-8:]
    with session_scope() as s:
        co = get_or_create_company(s, slug, "Shivchem Agro Limited")
        co.meta = {"bse_ipo_no": 8008, "exchange": "BSE"}
    run_id = create_run(slug)
    monkeypatch.setattr(bse, "BseClient", FakeBse)
    monkeypatch.setattr("finresearch.fincalc.dates.today_ist", lambda: date(2026, 9, 29))
    pipe = IpoPipeline(run_id)
    pipe.ctx = pipe._load_context()
    await pipe._facts()
    f = pipe.ctx.facts
    assert "issue_info_error" not in f, f.get("issue_info_error")
    assert (f["issue_open"], f["issue_close"], f["bidding_day_today"]) == ("2026-09-28", "2026-09-30", 2)
    with session_scope() as s:
        cs = s.scalars(select(Claim).where(Claim.run_id == run_id)).all()
        assert {c.metric for c in cs} >= {"price_band_upper", "lot_size"}
        assert {c.checks["source"] for c in cs} == {"bse_issue_info"}
        assert all(c.citations[0].url.endswith("GetMkt_ISSUE_BBS_IPO/w?IPO_NO=8008") for c in cs)


def test_symbol_search_and_quote_header():
    assert parse_smart_search((FIX / "smart_search_INFY.json").read_text()) == [
        ("500209", "INFOSYS LTD", "INFY")
    ]
    assert parse_smart_search("\"<li class='quotemenu'><a>No Match Found<br /><span></span></a></li>\"") == []
    q = parse_scrip_header("INFY", load("scrip_header_500209_20260929.json"))
    assert (q.open, q.previous_close, q.status) == (Decimal("1004.95"), Decimal("1003.00"), "Listed")
    assert q.as_of == datetime(2026, 9, 29, 11, 31, tzinfo=IST)
    assert q.listing_date is None  # a previous close: not the listing day
    assert parse_scrip_header("SHIVCHEM", load("scrip_header_unlisted_4858.json")) is None


def test_the_first_trading_day_is_the_listing_date():
    data = load("scrip_header_500209_20260929.json")
    data["Header"] = {**data["Header"], "PrevClose": None, "Ason": "05 Oct 26 | 10:15"}
    assert parse_scrip_header("INFY", data).listing_date == date(2026, 10, 5)


# --------------------------------------------------------------------------- monitor
async def test_bse_only_company_is_watched_from_bse_and_lists_on_bse(env, tmp_path):
    from finresearch.adapters.nse import Quote
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob, Watch
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.monitor.jobs import Deps, NotYet, listing
    from finresearch.monitor.watch import watch_company

    slug = "bseonly-" + tmp_path.name[-8:]
    with session_scope() as s:
        co = get_or_create_company(s, slug, "Shivchem Agro Limited")
        co.meta = {"bse_ipo_no": 8008, "exchange": "BSE"}

    async def fetch_bse(ipo_no):
        assert ipo_no == 8008
        return shivchem()

    async def no_nse(symbol):
        raise AssertionError("a BSE-only issue must not ask NSE")

    w = await watch_company(slug, fetch_detail=no_nse, fetch_bse=fetch_bse)
    assert w["nse_symbol"] == "SHIVCHEM" and w["close_date"] == shivchem().issue_close.isoformat()

    quotes = [None, Quote(symbol="SHIVCHEM", open=Decimal("70"), last_price=Decimal("72"),
                          listing_date=date(2026, 10, 5), as_of=datetime(2026, 10, 5, 10, 15, tzinfo=IST))]  # fmt: skip

    async def bse_quote(symbol):
        return quotes.pop(0)

    deps = Deps(ipo_detail=no_nse, quote=no_nse, bse_quote=bse_quote)
    with session_scope() as s:
        watch = s.get(Watch, w["id"])
        assert watch.meta["bse_ipo_no"] == 8008
        job = MonitorJob(watch_id=watch.id, kind="listing", slot=f"SHIVCHEM:listing:{tmp_path.name}",
                         due_at=datetime(2026, 10, 5, 10, 15, tzinfo=IST), params={"which": "open"})  # fmt: skip
        s.add(job)
        s.flush()
        with pytest.raises(NotYet, match="not listed on BSE"):
            await listing(s, job, watch, deps, datetime(2026, 10, 5, 4, 45, tzinfo=IST))
        out = await listing(s, job, watch, deps, datetime(2026, 10, 5, 4, 50, tzinfo=IST))
        s.flush()
        assert out["price"] == "70" and watch.listing_date == date(2026, 10, 5)
        assert s.query(Alert).filter_by(watch_id=watch.id, kind="listing_open").count() == 1


async def test_subscription_job_for_a_bse_watch_reads_the_bse_book(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob, SubscriptionSnapshotRow, Watch
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.monitor.jobs import Deps, subscription

    calls = []

    async def nse_detail(symbol):
        raise AssertionError("a BSE SME watch must not ask NSE")

    async def bse_detail(ipo_no):
        calls.append(ipo_no)
        return as_ipo_detail(shivchem(), shivchem_demand())

    with session_scope() as s:
        s.query(SubscriptionSnapshotRow).filter_by(nse_symbol="SHIVCHEM").delete()
        co = get_or_create_company(s, "bsewatch-" + tmp_path.name[-8:], "Shivchem Agro Limited")
        w = Watch(company_id=co.id, nse_symbol="SHIVCHEM", open_date=date(2026, 9, 28), close_date=date(2026, 9, 30),
                  meta={"bse_ipo_no": 8008})  # fmt: skip
        s.add(w)
        s.flush()
        job = MonitorJob(watch_id=w.id, kind="subscription", slot=f"SHIVCHEM:subscription:{tmp_path.name}",
                         due_at=datetime(2026, 9, 30, 17, 15, tzinfo=IST), params={"final": True})  # fmt: skip
        s.add(job)
        s.flush()
        out = await subscription(s, job, w, Deps(ipo_detail=nse_detail, quote=None, bse_ipo_detail=bse_detail),
                                 datetime(2026, 9, 30, 17, 15, tzinfo=IST))  # fmt: skip
        s.flush()
        snap = s.query(SubscriptionSnapshotRow).filter_by(nse_symbol="SHIVCHEM").one()
        assert (snap.source, snap.total_times) == ("bse_sme", Decimal("0.1301"))
        final = s.query(Alert).filter_by(watch_id=w.id, kind="subscription_final").one().message
    assert calls == [8008] and out["rii_times"] == "0.1445"
    assert "retail 0.14x" in final and "BSE SME book" in final


async def test_no_demand_schedule_yet_is_an_empty_book_not_an_error():
    import httpx

    from finresearch.adapters.bse import BseClient
    from finresearch.adapters.http import PoliteClient

    http = PoliteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"table": []})),
                        cache_dir=None)  # fmt: skip
    async with BseClient(http) as bse:
        snap = await bse.sme_subscription(8023, "TNA")
    await http.aclose()
    assert snap.total_times is None and snap.as_of is None and snap.categories == []

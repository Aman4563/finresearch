"""IPO history harvester (roadmap item 2): parsing, recorded NSE payloads for three historical issues, resumable and
idempotent upserts, export/import and the coverage report. Offline: NSE is mocked with respx."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NSE_BASE, NseClient, PastIssue
from finresearch.evals import ipo_history as ih

FIX = Path(__file__).parent / "fixtures" / "nse" / "ipo_history"
SYMBOLS = ("TEAMLEASE", "LAURUSLABS", "SWIGGY")


def load(name: str):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def clean(env):
    """This module's tables start empty (the test database is shared by the whole session)."""
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast, IpoHistory, SubscriptionArchiveSlot, SubscriptionSnapshotRow

    with session_scope() as s:
        for model in (Forecast, IpoHistory, SubscriptionArchiveSlot, SubscriptionSnapshotRow):
            s.query(model).delete()
    return env


# --------------------------------------------------------------------------- pure parsing
def test_issue_size_text_variants():
    t = ih.parse_issue_size("Public Issue of [.] Equity shares consisting of a fresh issue aggregating upto Rs 1,500 "
                            "million & an offer for sale upto 32,19,733 equity shares (including Anchor Portion "
                            "22,38,498 equity shares)", Decimal(850))  # fmt: skip
    assert t == {"issue_size_cr": Decimal("423.68"), "fresh_cr": Decimal("150.00"), "ofs_cr": Decimal("273.68"),
                 "ofs_share": Decimal("0.6460")}  # 3,219,733 x ₹850 = ₹273.68 cr  # fmt: skip
    t = ih.parse_issue_size("Initial Public offer comprising of Fresh issue aggregating up to Rs. 3,200 million and "
                            "Offer for Sale aggregating up to Rs. 2,320 million (including Anchor investor portion of "
                            "60,88,233 Equity shares)", Decimal(272))  # fmt: skip
    assert (t["fresh_cr"], t["ofs_cr"], t["ofs_share"]) == (
        Decimal("320.00"),
        Decimal("232.00"),
        Decimal("0.4203"),
    )
    t = ih.parse_issue_size('"Initial Public offer of [.] Equity Shares aggregating upto Rs 5,000 Million (including '
                            'anchor portion of 56,49,718 Equity Shares)"', Decimal(178))  # fmt: skip
    assert (
        t["issue_size_cr"] == Decimal("500.00") and t["ofs_share"] is None
    )  # split unknown: never assumed 0
    assert ih.parse_issue_size("Further Public offer of [.] Equity Shares aggregating upto Rs. 4,30,000 Lakhs",
                               Decimal(650))["issue_size_cr"] == Decimal("4300.00")  # fmt: skip
    assert ih.parse_issue_size(None, None)["issue_size_cr"] is None


def test_nifty_return_and_ipo_count_windows():
    closes = [(date(2024, 1, d), Decimal(100 + d)) for d in range(1, 31)]
    # 20 sessions ending on the anchor close (inclusive) vs strictly before it
    assert ih.nifty_return(closes, date(2024, 1, 25), inclusive=True) == (Decimal(125) / Decimal(105) - 1).quantize(
        Decimal("0.000001"))  # fmt: skip
    assert ih.nifty_return(closes, date(2024, 1, 25), inclusive=False) == (Decimal(124) / Decimal(104) - 1).quantize(
        Decimal("0.000001"))  # fmt: skip
    assert ih.nifty_return(closes, date(2024, 1, 10), inclusive=True) is None  # not 20 sessions yet
    assert ih.nifty_return(closes, date(2024, 3, 1), inclusive=True) is None  # a data gap is not bridged
    days = [date(2024, 1, 1), date(2024, 3, 1), date(2024, 3, 31), date(2024, 4, 1)]
    assert ih.ipo_count(days, date(2024, 4, 1)) == 2  # 90 days back, the anchor day excluded


def test_eligible_keeps_listed_ipos_of_one_board_only():
    rows = [PastIssue.parse(r) for r in load("public_past_issues.json")]
    eq = {r.symbol for r in ih.eligible(rows, "EQ", date(2026, 9, 30))}
    assert eq == set(
        SYMBOLS
    )  # the FPO (RUCHISOYA, "listed" in 2003), SME, debt and not-yet-listed rows are out
    assert len(ih.eligible(rows, "SME", date(2026, 9, 30))) == 1


# --------------------------------------------------------------------------- harvest over recorded payloads
def mock_nse(router: respx.MockRouter) -> dict[str, respx.Route]:
    router.get(re.compile(rf"{re.escape(NSE_BASE)}/(market-data|get-quotes)/.*")).mock(
        return_value=httpx.Response(200, text="<html>ok</html>"))  # fmt: skip
    router.get(f"{NSE_BASE}/api/public-past-issues").mock(
        return_value=httpx.Response(200, json=load("public_past_issues.json")))  # fmt: skip

    def detail(request):
        sym = request.url.params["symbol"]
        return httpx.Response(200, json=load(f"ipo_detail_{sym}.json"))

    def hist(request):
        sym = request.url.params["symbol"]
        if request.url.params["series"] != "EQ":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=load(f"history_{sym}.json"))

    return {"detail": router.get(f"{NSE_BASE}/api/ipo-detail").mock(side_effect=detail),
            "history": router.get(f"{NSE_BASE}/api/NextApi/apiClient/GetQuoteApi").mock(side_effect=hist),
            "nifty": router.get(f"{NSE_BASE}/api/historicalOR/indicesHistory").mock(
                return_value=httpx.Response(200, json=load("nifty50_windows.json")))}  # fmt: skip


async def _noop(_s: float) -> None:
    return None


def client() -> NseClient:
    return NseClient(PoliteClient(cache_dir=None, host_rates={"nseindia.com": 1e6}, sleep=_noop))


async def test_harvest_records_three_historical_issues_and_is_idempotent(clean):
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import IpoHistory

    logs: list[str] = []
    with respx.mock(assert_all_called=False) as router:
        routes = mock_nse(router)
        out = await ih.harvest(client=client(), today=date(2026, 9, 30), log=logs.append)
        assert out == {"series": "EQ", "fetched": 3, "complete": 3}
        detail_calls = routes["detail"].call_count
        again = await ih.harvest(client=client(), today=date(2026, 9, 30), log=logs.append)
        assert (
            again["fetched"] == 0 and routes["detail"].call_count == detail_calls
        )  # complete rows are skipped
        await ih.harvest(client=client(), today=date(2026, 9, 30), refresh=True, log=logs.append)
    with session_scope() as s:
        rows = {r.symbol: r for r in s.scalars(select(IpoHistory))}
        assert sorted(rows) == sorted(SYMBOLS)  # upserts, no duplicates
        tl = rows["TEAMLEASE"]
        # listing 12-Feb-2016: issue ₹850 (list and listing-day previous close agree), open ₹860
        assert (tl.listing_date, tl.issue_price, tl.list_open, tl.list_prev_close) == (
            date(2016, 2, 12), Decimal(850), Decimal(860), Decimal(850))  # fmt: skip
        assert tl.return_open == Decimal("0.011765") and tl.post_2022 is False
        assert tl.qib_times == Decimal("26.9713") and tl.subscription_scope == "nse_combined"
        assert (tl.issue_size_cr, tl.ofs_share) == (Decimal("423.68"), Decimal("0.6460"))
        assert tl.status == "complete" and tl.nifty_ret20_close is not None
        lau = rows["LAURUSLABS"]
        assert lau.qib_times == Decimal("10.5370") and lau.employee_times == Decimal("1.7058")
        assert lau.issue_size_cr == Decimal("1331.80") and lau.return_open == Decimal(
            "0.144626"
        )  # 489.9/428 - 1
        sw = rows["SWIGGY"]
        assert sw.post_2022 is True and sw.bnii_times is not None and sw.snii_times is not None
        assert sw.return_open is not None and sw.source["ipo_detail"]["url"].endswith("series=EQ")
        # the coverage report counts what is there and what is missing, by series and year
        cov = ih.coverage(list(rows.values()))
        assert cov["EQ"]["rows"] == 3 and cov["EQ"]["with_listing_open"] == 3 and cov["EQ"]["with_book"] == 3
        assert set(cov["EQ"]["by_year"]) == {"2016", "2024"}
        # re-deriving from the stored raw payloads (no network) gives the same row
        before = {
            c.name: getattr(tl, c.name) for c in IpoHistory.__table__.columns if c.name not in ("updated_at",)
        }
        assert ih.reparse(s) == 3
        s.flush()
        after = {
            c.name: getattr(tl, c.name) for c in IpoHistory.__table__.columns if c.name not in ("updated_at",)
        }
        assert after == before


async def test_export_and_import_round_trip(clean, tmp_path):
    from sqlalchemy import func, select

    from finresearch.db import session_scope
    from finresearch.db.models import IpoHistory

    with respx.mock(assert_all_called=False) as router:
        mock_nse(router)
        await ih.harvest(client=client(), today=date(2026, 9, 30), log=lambda _m: None)
    path = tmp_path / "ipo_history.jsonl"
    with session_scope() as s:
        assert ih.export_jsonl(s, path) == 3
        s.execute(IpoHistory.__table__.delete())
    with session_scope() as s:
        assert ih.import_jsonl(s, path) == 3
        assert s.scalar(select(func.count()).select_from(IpoHistory)) == 3
        tl = s.scalar(select(IpoHistory).where(IpoHistory.symbol == "TEAMLEASE"))
        assert tl.return_open == Decimal("0.011765") and tl.raw["active"]["dataList"]
    rows = ih.load_jsonl(path)
    assert {r["symbol"] for r in rows} == set(SYMBOLS) and isinstance(datetime.now(), datetime)


def test_fetch_failure_is_recorded_not_fatal():
    x = ih.Inputs("GONE", "EQ", "Gone Ltd", date(2020, 1, 1), date(2020, 1, 3), date(2020, 1, 8), Decimal(100), None,
                  None, {}, None, [])  # fmt: skip
    row = ih.derive(x, [], [])
    assert row["status"] == "partial" and any(m.startswith("subscription") for m in row["missing"])
    assert any(m.startswith("listing bar") for m in row["missing"]) and row["return_open"] is None


@pytest.mark.parametrize("sym", SYMBOLS)
def test_fixtures_are_trimmed_recordings(sym):
    d = load(f"ipo_detail_{sym}.json")
    assert d["activeCat"]["dataList"] and d["metaInfo"]["listingDate"]
    assert load(f"history_{sym}.json")

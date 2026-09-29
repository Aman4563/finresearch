"""IPO lot sizes and per-category application amounts: NSE issue information (primary), BSE issue details
(cross-check / fallback), the ICDR arithmetic and the radar rows. Fixtures recorded 28/29-Sep-2026."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.bse import BseIssueDetail
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import (
    NSE_BASE,
    SME_MIN_BASIS,
    WARMUP_URL,
    IpoIssue,
    NseClient,
    parse_ipo_detail,
    parse_issue_info,
    parse_issue_terms,
)
from finresearch.api.radar import terms_fields
from finresearch.fincalc.ipo import application_limits

FIX = Path(__file__).parent / "fixtures"


def load(path: str):
    return json.loads((FIX / path).read_text())


def nse_terms(name: str, symbol: str, series: str = "EQ"):
    return parse_issue_terms(symbol, parse_issue_info(load(f"nse/{name}").get("issueInfo")), series)


def orient_bse() -> BseIssueDetail:
    return BseIssueDetail.parse(load("bse/issue_detail_8001_ORIENTCABL_mainboard_20260929.json"))


# --------------------------------------------------------------------------- parsing
def test_mainboard_terms_come_from_bid_lot_and_minimum_order_quantity():
    t = parse_ipo_detail("ORIENTCABL", load("nse/ipo_detail_ORIENTCABL_20260928_1443.json")).terms
    assert (t.lot_size, t.min_bid_shares, t.min_lots) == (55, 55, 1)
    assert (t.price_low, t.price_high) == (Decimal(258), Decimal(272))
    assert t.min_lots_basis == "NSE Minimum Order Quantity" and not t.is_sme


def test_sme_terms_read_lot_size_and_apply_the_two_lot_minimum():
    t = nse_terms("ipo_detail_BMISL_SME_20260929.json", "BMISL", "SME")
    assert (t.lot_size, t.min_lots, t.min_bid_shares) == (1200, 2, 2400)
    assert t.min_lots_basis == SME_MIN_BASIS and t.price_high == Decimal(110)
    g = nse_terms("ipo_detail_GREENASIA_SME_issue_info_20260929.json", "GREENASIA", "SME")
    assert g.lot_size == 1600 and g.price_range == "Rs.84 to Rs.89 per equity share"  # "... and in multiples"


def test_retail_cap_is_read_from_the_issue_page():
    t = nse_terms("ipo_detail_RUNWALENTR_issue_info_20260929.json", "RUNWALENTR")
    assert t.lot_size == 49 and t.retail_cap == Decimal(200000) and t.price_high == Decimal(305)


def test_bse_mainboard_details_carry_the_same_lot():
    d = orient_bse()
    assert (d.symbol, d.market_lot, d.minimum_bid, d.price_high) == ("ORIENTCABL", 55, 55, Decimal(272))


# --------------------------------------------------------------------------- arithmetic
def test_mainboard_application_limits_orient_cables():
    lim = application_limits(55, 272)
    assert lim.lot_cost == 14960 and lim.min_investment == 14960
    assert (lim.retail_max_lots, lim.retail_max_amount) == (13, 194480)
    assert (lim.shni_min_lots, lim.shni_min_amount) == (14, 209440)
    assert (lim.bhni_min_lots, lim.bhni_min_amount) == (67, 1002320)


def test_sme_application_limits_bmisl():
    lim = application_limits(1200, 110, min_lots=2, sme=True)
    assert lim.lot_cost == 132000 and lim.min_investment == 264000
    assert lim.retail_max_lots == 2  # individuals bid exactly the minimum
    assert (lim.shni_min_lots, lim.shni_min_amount) == (3, 396000)
    assert (lim.bhni_min_lots, lim.bhni_min_amount) == (8, 1056000)


def test_limits_edge_cases():
    exact = application_limits(100, 2000)  # one lot is exactly ₹2 lakh: still retail
    assert exact.retail_max_lots == 1 and exact.shni_min_lots == 2 and exact.bhni_min_lots == 6
    big = application_limits(100, 5000)  # one lot is ₹5 lakh: no retail bid fits
    assert big.retail_max_lots is None and big.shni_min_lots == 1 and big.bhni_min_lots == 3
    huge = application_limits(100, 20000, min_lots=2, sme=True)  # 3 lots already above ₹10 lakh
    assert huge.shni_min_lots is None and huge.bhni_min_lots == 3
    with pytest.raises(ValueError):
        application_limits(0, 100)


# --------------------------------------------------------------------------- radar rows
def test_row_uses_nse_and_reports_bse_agreement():
    t = parse_ipo_detail("ORIENTCABL", load("nse/ipo_detail_ORIENTCABL_20260928_1443.json")).terms
    f = terms_fields(series="EQ", list_band="Rs.258 to Rs.272", nse=t, bse=orient_bse())
    assert (f["lot_size"], f["min_lots"], f["min_bid_shares"]) == (55, 1, 55)
    a = f["application"]
    assert (
        a["lot_cost"],
        a["min_investment"],
        a["retail_max_lots"],
        a["shni_min_lots"],
        a["bhni_min_lots"],
    ) == (
        14960,
        14960,
        13,
        14,
        67,
    )
    assert f["lot_source"]["label"] == "NSE issue information" and f["lot_source"]["check"] == "BSE agrees"
    assert f["price_band_note"] is None and "price_band" not in f


def test_row_falls_back_to_bse_when_the_nse_page_fails():
    f = terms_fields(
        series="EQ", list_band="Rs.258 to Rs.272", bse=orient_bse(), note="NSE issue page: NseError"
    )
    assert f["lot_size"] == 55 and f["lot_source"]["label"] == "BSE issue details"
    assert f["application"]["lot_cost"] == 14960 and f["lot_note"] == "NSE issue page: NseError"


def test_row_flags_a_stale_list_band_and_prices_at_the_issue_page():
    row = load("nse/ipo_current_issue_RUNWALENTR_20260929.json")[0]
    issue = IpoIssue.parse(row)
    t = nse_terms("ipo_detail_RUNWALENTR_issue_info_20260929.json", "RUNWALENTR")
    f = terms_fields(series="EQ", list_band=issue.price_band, nse=t)
    assert issue.price_band == "Rs.290 to Rs.302" and f["price_band"] == "Rs.290 to Rs.305"
    assert f["price_band_list"] == "Rs.290 to Rs.302" and "Rs.290 to Rs.305" in f["price_band_note"]
    assert f["application"]["lot_cost"] == 49 * 305


def test_row_fills_a_missing_sme_band_and_notes_an_unpublished_lot():
    t = nse_terms("ipo_detail_BMISL_SME_20260929.json", "BMISL", "SME")
    f = terms_fields(series="SME", list_band=None, nse=t)
    assert f["price_band"] == "Rs.104 to Rs.110" and f["application"]["min_investment"] == 264000
    assert f["lot_source"]["min_lots_basis"] == SME_MIN_BASIS and f["lot_source"]["check"] is None
    empty = terms_fields(series="EQ", list_band="Rs.10 to Rs.12")
    assert empty["lot_size"] is None and empty["application"] is None and "not published" in empty["lot_note"]


# --------------------------------------------------------------------------- client caching
@respx.mock
async def test_issue_terms_are_cached_only_once_the_lot_is_published(tmp_path):
    respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    page = load("nse/ipo_detail_RUNWALENTR_issue_info_20260929.json")
    no_lot = {"issueInfo": {"dataList": [i for i in page["issueInfo"]["dataList"] if "Lot" not in (i["title"] or "")
                                         and "Minimum" not in (i["title"] or "")]}}  # fmt: skip
    route = respx.get(f"{NSE_BASE}/api/ipo-detail").mock(
        side_effect=[
            httpx.Response(200, text="<html>blocked</html>"),  # 200 block page: re-warm, never cached
            httpx.Response(200, json=no_lot),
            httpx.Response(200, json=page),
            httpx.Response(200, json=page),
        ]
    )
    async with NseClient(PoliteClient(cache_dir=tmp_path / "c", backoff_base=0.0)) as nse:
        first = await nse.issue_terms("RUNWALENTR", "EQ")
        second = await nse.issue_terms("RUNWALENTR", "EQ")
        third = await nse.issue_terms("RUNWALENTR", "EQ")
    assert first.lot_size is None and second.lot_size == third.lot_size == 49
    assert route.call_count == 3 and second.source_url and second.as_of is not None

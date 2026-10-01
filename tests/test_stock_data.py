"""Listed-stock data: NSE equity payload parsers (recorded), results XBRL and market arithmetic (golden values)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.nse_equity import (
    Announcement,
    CorporateAction,
    IntegratedFiling,
    NseEquity,
    PriceBar,
    ResultFiling,
    Shareholding,
)
from finresearch.adapters.xbrl import parse_results_xbrl
from finresearch.fincalc import market
from finresearch.fincalc.dates import fiscal_quarter_label, fiscal_year

EQ = Path(__file__).parent / "fixtures" / "nse" / "equity"


def load(name):
    return json.loads((EQ / name).read_text())


def test_price_history_parses_and_sorts():
    bars = sorted(
        (PriceBar.parse(r) for r in load("history_INFY_20260901_20260928.json")), key=lambda b: b.day
    )
    last = bars[-1]
    assert last.day == date(2026, 9, 28) and last.close == Decimal("1003.2") and last.open == Decimal("1002")
    assert last.week52_high == Decimal("1728") and last.volume == Decimal("8567672") and len(bars) == 19
    assert bars[0].day < bars[-1].day


def test_announcements_find_results_filings():
    anns = [Announcement.parse(r) for r in load("announcements_INFY_trimmed.json")]
    results = [a for a in anns if a.results_period_end]
    assert [a.results_period_end for a in results] == [
        date(2026, 6, 30),
        date(2026, 3, 31),
        date(2025, 12, 31),
    ]
    assert results[0].attachment.startswith("https://nsearchives.nseindia.com/corporate/Infosys_23072026")
    assert anns[0].category == "Allotment of Securities" and anns[0].results_period_end is None


def test_results_filings_shareholding_and_corporate_actions():
    rf = ResultFiling.parse(load("results_INFY_trimmed.json")[0])
    assert rf.period_to == date(2024, 12, 31) and rf.audited is True and rf.xbrl.endswith(".xml")
    sh = Shareholding.parse(load("shareholding_INFY.json")[0])
    assert (sh.as_of, sh.promoter_pct, sh.public_pct) == (
        date(2026, 6, 30),
        Decimal("13.82"),
        Decimal("85.97"),
    )
    acts = [CorporateAction.parse(r) for r in load("actions_INFY.json")]
    div = acts[0]
    assert div.subject == "Dividend - Rs 25 Per Share" and div.dividend_per_share == 25
    assert div.ex_date == date(2026, 6, 10) and acts[1].dividend_per_share is None  # buyback


def test_results_xbrl_reads_the_quarter_and_the_true_year_to_date_period():
    x = parse_results_xbrl((EQ / "results_INFY_Q3FY25_consolidated.xml").read_bytes())
    q, ytd = x.quarter, x.year_to_date
    assert x.symbol == "INFY" and x.consolidated and x.audited
    assert (q.start, q.end) == (date(2024, 10, 1), date(2024, 12, 31))
    assert q.facts["revenue_from_operations"] == Decimal("417640000000.00")  # ₹41,764 cr, as Infosys reported
    assert q.facts["profit_for_period"] == Decimal("68220000000.00") and q.facts["eps_basic"] == Decimal(
        "16.43"
    )
    # the FourD context says Oct–Dec in xbrli:period, but the filing's own period facts say Apr–Dec
    assert (ytd.start, ytd.end) == (date(2024, 4, 1), date(2024, 12, 31))
    assert ytd.facts["revenue_from_operations"] == Decimal("1220640000000.00")
    assert "segment_revenue" not in q.facts  # dimensioned (segment) facts are not mixed in


def test_integrated_filing_rows_parse_and_merge_into_result_filings():
    """NSE's integrated-filing index (results since the Mar-2025 quarter): upper-case quarter dates, a literal
    '/corporate/null' for a missing PDF, and governance rows mixed in with the financials."""
    rows = [IntegratedFiling.parse(r) for r in load("integrated_filings_INFY_trimmed.json")["data"]]
    gov, sa, con = rows[0], rows[1], rows[2]
    assert gov.kind == "Integrated Filing- Governance" and gov.consolidated is None
    assert (
        con.kind == "Integrated Filing- Financials" and con.consolidated is True and sa.consolidated is False
    )
    assert con.period_end == date(2026, 6, 30) and con.audited is True and con.pdf is None
    assert con.filed_at.isoformat() == "2026-07-23T17:40:57+05:30" and con.ixbrl.endswith("_iXBRL_WEB.html")
    rf = con.as_result_filing()
    assert (rf.period_from, rf.period_to, rf.consolidated) == (date(2026, 4, 1), date(2026, 6, 30), True)
    assert rf.source == "nse_integrated_filing" and rf.xbrl == con.xbrl and not rf.revised
    assert rows[3].as_result_filing().period_from == date(2026, 1, 1)


def test_integrated_filing_xbrl_parses_with_the_same_facts():
    x = parse_results_xbrl((EQ / "integrated_INFY_Q1FY27_consolidated.xml").read_bytes())
    assert (
        x.symbol == "500209" and x.consolidated is True and x.audited is True
    )  # identifier = BSE scrip code
    q = x.quarter
    assert (q.start, q.end) == (date(2026, 4, 1), date(2026, 6, 30)) and x.year_to_date is None
    f = q.facts
    assert f["revenue_from_operations"] == Decimal("482110000000") and f["total_expenses"] == Decimal(
        "381670000000"
    )
    assert f["profit_before_tax"] == Decimal("110280000000") and f["tax_expense"] == Decimal("32530000000")
    assert f["profit_attributable_to_owners"] == Decimal("77690000000") and f["eps_basic"] == Decimal("19.19")
    y = parse_results_xbrl((EQ / "integrated_INFY_Q4FY26_consolidated.xml").read_bytes()).year_to_date
    assert (y.start, y.end) == (date(2025, 4, 1), date(2026, 3, 31)) and y.facts["exceptional_items"] < 0


def test_bank_results_xbrl_maps_the_banking_taxonomy():
    x = parse_results_xbrl((EQ / "integrated_HDFCBANK_Q1FY27_consolidated.xml").read_bytes())
    f = x.quarter.facts
    assert "revenue_from_operations" not in f and f["interest_earned"] == Decimal("905753300000")
    assert f["profit_before_tax"] == Decimal("271931600000") and f["profit_for_period"] == Decimal(
        "203826900000"
    )
    assert f["profit_attributable_to_owners"] == Decimal("192447100000") and f["eps_basic"] == Decimal("12.5")
    assert f["expenditure_excluding_provisions"] + f["provisions"] == Decimal("1059172000000")


def test_fiscal_quarter_labels():
    assert (
        fiscal_quarter_label(date(2026, 6, 30)) == "Q1 FY27"
        and fiscal_quarter_label(date(2026, 3, 31)) == "Q4 FY26"
    )
    assert (
        fiscal_quarter_label(date(2025, 12, 31)) == "Q3 FY26"
        and fiscal_quarter_label(date(2024, 9, 30)) == "Q2 FY25"
    )
    assert fiscal_year(date(2026, 4, 1)) == 2027 and fiscal_year(date(2026, 3, 31)) == 2026


def test_market_arithmetic_golden_values():
    closes = [100, 110, 99, 121]
    assert market.price_return(100, 121) == Decimal("0.21")
    assert market.total_return(100, 121, 4) == Decimal("0.25")
    r = market.daily_returns(closes)
    assert r[:2] == [Decimal("0.1"), Decimal("-0.1")] and round(r[2], 10) == Decimal("0.2222222222")
    dd = market.max_drawdown(closes)
    assert (dd.max_drawdown, dd.peak_index, dd.trough_index) == (Decimal("-0.1"), 1, 2)
    assert market.max_drawdown([1, 2, 3]).max_drawdown == 0
    assert market.moving_average(closes, 2) == Decimal("110")
    # returns 0.1, -0.1, 0.2222 (mean 0.0741): squared deviations sum 0.052922, /2 = 0.026461, sd 0.162668,
    # x sqrt(252) = 2.5823
    assert round(market.annualised_volatility(closes), 4) == Decimal("2.5823")
    assert market.dividend_yield(25, 1003.2).quantize(Decimal("0.0001")) == Decimal("0.0249")
    assert market.range_position(1003.2, 982.4, 1728) == (Decimal("1003.2") - Decimal("982.4")) / (
        Decimal("1728") - Decimal("982.4"))  # fmt: skip
    assert market.dividend_per_share("Interim Dividend - Rs 21.50 Per Share") == Decimal("21.50")
    assert market.dividend_per_share("Bonus 1:1") is None
    with pytest.raises(ValueError):
        market.annualised_volatility([100, 101])


@respx.mock
async def test_nse_equity_warms_the_quote_page_and_sends_it_as_referer():
    respx.get("https://www.nseindia.com/get-quotes/equity", params={"symbol": "INFY"}).mock(
        return_value=httpx.Response(200, text="<html>quote</html>"))  # fmt: skip
    route = respx.get("https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi").mock(
        return_value=httpx.Response(200, json=load("history_INFY_20260901_20260928.json")))  # fmt: skip
    async with NseEquity() as eq:
        bars = await eq.history("INFY", date(2026, 9, 1), date(2026, 9, 28))
    req = route.calls.last.request
    assert (
        req.url.params["functionName"] == "getHistoricalTradeData"
        and req.url.params["fromDate"] == "01-09-2026"
    )
    assert req.headers["referer"] == "https://www.nseindia.com/get-quotes/equity?symbol=INFY"
    assert len(bars) == 19 and bars[-1].day == date(2026, 9, 28)


# --------------------------------------------------------------------------- shareholding-pattern XBRL
@pytest.mark.parametrize(
    ("day", "taxonomy", "promoter", "fpi", "dii", "retail", "hni"),
    [
        ("30-JUN-2026", "2025-10-31", "13.82", "27.09", "42.96", "8.38", "4.61"),  # fractions (0.1382)
        ("31-MAR-2025", "2022-09-30", "14.60", "32.88", "38.52", "7.42", "4.24"),  # percents
        ("30-SEP-2021", "2020-09-30", "13.12", "33.46", "15.66", "6.27", "4.26"),  # one "Institutions" block
    ],
)
def test_shareholding_xbrl_categories_across_taxonomy_vintages(
    day, taxonomy, promoter, fpi, dii, retail, hni
):
    from finresearch.adapters.shp_xbrl import parse_shareholding_xbrl

    p = parse_shareholding_xbrl((EQ / f"shp_INFY_{day}.xml").read_bytes())
    assert p.symbol == "INFY" and p.taxonomy == taxonomy and p.warnings == []
    assert p.as_of.strftime("%d-%b-%Y").upper() == day
    s, g = p.split, p.groups
    assert s["promoter"] == Decimal(promoter) and s["fpi"] == Decimal(fpi) and g["dii"] == Decimal(dii)
    assert s["retail"] == Decimal(retail) and s["hni"] == Decimal(hni)
    # the parts add back to the filed subtotals and the whole statement to 100 %
    r = p.raw
    assert s["promoter"] + r["public"] + s["employee_trusts"] == Decimal(100)
    assert abs(sum(v for v in g.values() if v is not None) - 100) <= Decimal("0.02")
    if "institutions_domestic" in r:
        assert s["mutual_funds"] + s["insurance"] + s["banks"] + s["other_dii"] == r["institutions_domestic"]
        assert s["fpi"] + s["foreign_other"] == r["institutions_foreign"]


def test_shareholding_xbrl_reports_depository_receipts_outside_the_basis():
    from finresearch.adapters.shp_xbrl import parse_shareholding_xbrl

    p = parse_shareholding_xbrl((EQ / "shp_INFY_30-JUN-2026.xml").read_bytes())
    # 31,76,58,095 ADR-underlying shares of 4,05,75,78,830 (BSE's summary says 7.83 %); the filed % exclude them
    assert p.dr_shares == 317658095 and p.total_shares == 4057578830
    assert round(p.dr_pct_of_total, 2) == Decimal("7.83")
    old = parse_shareholding_xbrl((EQ / "shp_INFY_30-SEP-2021.xml").read_bytes())
    assert old.split["depositories"] == Decimal("17.74")  # before 2022 the ADRs were counted as public


def test_shareholding_xbrl_without_category_rows_is_flagged():
    from finresearch.adapters.shp_xbrl import parse_shareholding_xbrl

    p = parse_shareholding_xbrl(b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>')
    assert p.split == {} and p.warnings == ["no category rows in the filing"]


def test_shareholding_xbrl_no_promoter_and_strategic_fdi():
    from finresearch.adapters.shp_xbrl import parse_shareholding_xbrl

    # ITC 30-Jun-2026: no promoter group; BAT's 22.91 % is filed as foreign direct investment, not as an FPI
    p = parse_shareholding_xbrl((EQ / "shp_ITC_30-JUN-2026.xml").read_bytes())
    s, g = p.split, p.groups
    assert s["promoter"] == 0 and s["fdi"] == Decimal("22.91") and s["fpi"] == Decimal("11.32")
    # SUUTI's 7.79 % is filed as an "other financial institution"
    assert g["fii"] == Decimal("11.32") and s["banks"] == Decimal("7.81")
    assert abs(sum(v for v in g.values() if v is not None) - 100) <= Decimal("0.02") and p.warnings == []


@respx.mock
async def test_integrated_filings_asks_for_financials_and_drops_other_kinds():
    respx.get("https://www.nseindia.com/get-quotes/equity", params={"symbol": "INFY"}).mock(
        return_value=httpx.Response(200, text="<html>quote</html>"))  # fmt: skip
    route = respx.get("https://www.nseindia.com/api/integrated-filing-results").mock(
        return_value=httpx.Response(200, json=load("integrated_filings_INFY_trimmed.json")))  # fmt: skip
    async with NseEquity() as eq:
        rows = await eq.integrated_filings("INFY")
    assert route.calls.last.request.url.params["type"] == "Integrated Filing- Financials"
    assert len(rows) == 4 and all(r.kind == "Integrated Filing- Financials" for r in rows)


@pytest.mark.parametrize(
    ("name", "revenue_key", "revenue", "pbt", "tax_key", "tax", "pat", "eps"),
    [  # life insurer: the shareholders' account tax, not the policyholders' one (4,725 lakh)
        ("integrated_SBILIFE_Q1FY27_standalone.xml", "net_premium_income", "200782091000", "7458746000",
         "tax_shareholders_account", "209415000", "7249331000", "7.22"),
        ("integrated_ICICIGI_Q1FY27_standalone.xml", "premium_earned", "59500400000", "5357000000",
         "provision_for_tax", "1325300000", "4031700000", "8.08"),
    ],
)  # fmt: skip
def test_insurer_results_xbrl_maps_premium_and_the_pnl_account(
    name, revenue_key, revenue, pbt, tax_key, tax, pat, eps
):
    f = parse_results_xbrl((EQ / name).read_bytes()).quarter.facts
    assert "revenue_from_operations" not in f and f[revenue_key] == Decimal(revenue)
    assert f["profit_before_tax"] == Decimal(pbt) and f[tax_key] == Decimal(tax)
    assert f["profit_before_tax"] - f[tax_key] == f["profit_for_period"] == Decimal(pat)
    assert f["eps_basic"] == Decimal(eps)


async def test_a_history_row_without_a_date_is_skipped_not_fatal():
    from finresearch.adapters.nse_equity import NseEquity

    class Fake(NseEquity):
        def __init__(self):
            pass

        async def _get(self, *a, **k):
            return [
                {"mtimestamp": "29-Sep-2026", "chClosingPrice": 100},
                {"mtimestamp": "-", "chClosingPrice": 0},
            ]

    bars = await Fake().history("EXAMPLE", date(2026, 9, 1), date(2026, 9, 30))
    assert [b.day for b in bars] == [date(2026, 9, 29)]

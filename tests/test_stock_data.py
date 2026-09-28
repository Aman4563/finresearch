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
    NseEquity,
    PriceBar,
    ResultFiling,
    Shareholding,
)
from finresearch.adapters.xbrl import parse_results_xbrl
from finresearch.fincalc import market

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

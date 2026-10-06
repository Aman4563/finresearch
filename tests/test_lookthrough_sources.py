"""#214: quant and Tata monthly portfolios (synthetic workbooks in their layouts), their discovery, the supported /
unsupported fund-house registry, the coverage figure and the monthly fetch. Offline only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from amc_synthetic import quant_xlsx, tata_xlsx

from finresearch.adapters.amc_portfolio import parse_as_of, parse_file


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
    assert (a.industry, a.weight, a.value_lakh, a.kind) == ("Auto Components", D("40.0"), D("400.0"), "equity")
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
    assert by_isin(p, "IN0020240019").kind == "govt" and p.grand_total_lakh == D("1000.0") and p.warnings == []
    assert (by_isin(p, "INE00TA01014").industry, by_isin(p, "INE00TA01014").weight) == ("Banks", D("55.0"))
    assert by_isin(p, "INE00TA01014").value_lakh == D("550.0")  # 'MKT VAL(Rs. Lacs)'
    assert by_isin(p, "INF00TC01AB3").kind == "mf_units"  # ETF units listed under equity are not a stock
    assert p.weight_of("equity") == D("85.0")
    assert parse_as_of("Portfolio as on 31-08-26") == date(2026, 8, 31)

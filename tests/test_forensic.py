"""Forensic scores (fincalc.forensic): golden values worked by hand from the published formulas, the bank/NBFC/insurer
guard, and missing inputs giving None with the list of missing lines (never a guess)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.fincalc import forensic as fz

EQ = Path(__file__).parent / "fixtures" / "nse" / "equity"

# a manufacturer, ₹ crore; every figure below is used in the hand calculations in the tests
CUR = {"revenue_from_operations": 1200, "cost_of_materials": 700, "purchases_stock_in_trade": 0,
       "changes_in_inventories": 0, "other_expenses": 120, "depreciation": 50, "ppe": 500,
       "trade_receivables_current": 240, "current_assets": 600, "total_assets": 1500, "current_investments": 100,
       "noncurrent_investments": 0, "current_liabilities": 300, "borrowings_noncurrent": 200, "cfo": 150,
       "profit_for_period": 100, "profit_continuing": 100, "proceeds_share_issue": 0, "total_equity": 900,
       "total_liabilities": 600, "other_equity": 800, "profit_before_tax": 140, "finance_costs": 20,
       "other_income": 10}  # fmt: skip
PREV = {"revenue_from_operations": 1000, "cost_of_materials": 600, "purchases_stock_in_trade": 0,
        "changes_in_inventories": 0, "other_expenses": 100, "depreciation": 45, "ppe": 450,
        "trade_receivables_current": 150, "current_assets": 500, "total_assets": 1200, "current_investments": 100,
        "noncurrent_investments": 0, "current_liabilities": 280, "borrowings_noncurrent": 220, "cfo": 120,
        "profit_for_period": 72, "proceeds_share_issue": 0, "profit_before_tax": 110, "finance_costs": 20,
        "other_income": 5}  # fmt: skip


def close(a, b, tol="0.00001"):
    return a is not None and abs(Decimal(a) - Decimal(b)) < Decimal(tol)


def test_beneish_m_score_golden():
    # DSRI (240/1200)/(150/1000) = 4/3; GMI 0.4/(500/1200) = 0.96; AQI (1-1200/1500)/(1-1050/1200) = 0.2/0.125 = 1.6
    # SGI 1.2; DEPI (45/495)/(50/550) = 1; SGAI 0.1/0.1 = 1; LVGI (500/1500)/(500/1200) = 0.8; TATA (100-150)/1500
    s = fz.beneish(CUR, PREV)
    c = s.components
    assert close(c["dsri"], Decimal(4) / 3) and close(c["gmi"], "0.96") and close(c["aqi"], "1.6")
    assert close(c["sgi"], "1.2") and close(c["depi"], 1) and close(c["sgai"], 1) and close(c["lvgi"], "0.8")
    assert close(c["tata"], Decimal(-50) / 1500)
    # M = -4.84 + 0.92(4/3) + 0.528(0.96) + 0.404(1.6) + 0.892(1.2) + 0.115 - 0.172 + 4.679(-1/30) - 0.327(0.8)
    assert close(s.value, "-1.86422", "0.000001")
    assert s.red_flag is False and s.flag == "no flag" and "SG&A" in s.proxies[0]
    # sales jump with receivables ballooning pushes it over -1.78
    hot = {**CUR, "trade_receivables_current": 480}
    assert fz.beneish(hot, PREV).red_flag is True


def test_altman_z_double_prime_em_golden():
    # X1 (600-300)/1500 = 0.2; X2 800/1500; X3 (140+20)/1500; X4 900/600 = 1.5
    s = fz.altman_z_em(CUR)
    expected = (
        Decimal("6.56") * Decimal("0.2")
        + Decimal("3.26") * Decimal(800) / 1500
        + Decimal("6.72") * Decimal(160) / 1500
        + Decimal("1.05") * Decimal("1.5")
    )
    assert close(s.value, expected) and close(s.value, "5.342467", "0.000001")
    assert s.flag == "safe" and close(s.components["em_score"], expected + Decimal("3.25"))
    distressed = {**CUR, "current_assets": 200, "other_equity": -300, "profit_before_tax": -100, "total_equity": 100,
                  "total_liabilities": 1400}  # fmt: skip
    d = fz.altman_z_em(distressed)
    assert d.value < Decimal("1.1") and d.flag == "distress" and d.red_flag


def test_piotroski_golden_and_not_applicable_gross_margin():
    s = fz.piotroski(CUR, PREV)
    c = s.components
    # ROA 100/1500 > 72/1200; CFO 150 > NI 100; LTD/TA 0.133 < 0.183; CR 2.0 > 1.786; GM 0.417 > 0.4;
    # asset turnover 0.8 < 0.833 -> the only miss
    assert s.value == 8 and c["out_of"] == 9 and c["asset_turnover_rose"] == 0 and s.flag == "strong"
    services = {**CUR, "cost_of_materials": 0}
    services_prev = {**PREV, "cost_of_materials": 0}
    t = fz.piotroski(services, services_prev)
    assert t.components["gross_margin_rose"] is None and t.components["out_of"] == 8 and t.value == 7
    assert any("gross margin not applicable" in p for p in t.proxies)


def test_accruals_and_cash_conversion_golden():
    a = fz.accruals_ratio(CUR, PREV)
    assert close(a.value, Decimal(-50) / 1350) and a.red_flag is False
    # EBITDA = PBT + finance + D&A - other income: 140+20+50-10 = 200 and 110+20+45-5 = 170; CFO 150+120
    c = fz.cfo_to_ebitda([PREV, CUR])
    assert close(c.value, Decimal(270) / 370) and c.flag == "healthy"
    weak = fz.cfo_to_ebitda([{**CUR, "cfo": 60}])
    assert close(weak.value, "0.3") and weak.red_flag


def test_missing_lines_give_none_with_the_list_never_a_guess():
    prev = {k: v for k, v in PREV.items() if k != "trade_receivables_current"}
    s = fz.beneish(CUR, prev)
    assert s.value is None and s.missing == ["trade_receivables_current (prior year)"]
    p = fz.piotroski({k: v for k, v in CUR.items() if k != "cfo"}, PREV)
    assert p.value is None and "cfo" in p.missing
    assert fz.altman_z_em({}).missing[:2] == ["current_assets", "current_liabilities"]


@pytest.mark.parametrize("kwargs", [
    {"industry": "Private Sector Bank"}, {"industry": "Non Banking Financial Company (NBFC)"},
    {"industry": "Life Insurance"}, {"revenue_basis": "interest_earned"},
])  # fmt: skip
def test_not_for_banks_nbfcs_or_insurers(kwargs):
    for s in fz.scorecard(CUR, PREV, **kwargs):
        assert s.value is None and s.reason and s.reason.startswith("Not meaningful for")
    assert all(
        s.value is not None for s in fz.scorecard(CUR, PREV, industry="Computers - Software & Consulting")
    )


def test_scores_from_a_real_integrated_filing():
    """INFY's FY26 annual integrated filing carries the balance sheet and cash flow the scores need."""
    from finresearch.adapters.xbrl import parse_results_xbrl

    x = parse_results_xbrl((EQ / "integrated_INFY_Q4FY26_consolidated.xml").read_bytes())
    bs = x.balance_sheet
    assert bs is not None and bs.end.isoformat() == "2026-03-31" and x.company_type == "Main Board"
    year = {**x.year_to_date.facts, **bs.facts}
    z = fz.altman_z_em(year)
    # X1 (103489-52322)/155967, X2 90828/155967, X3 (39995+416)/155967, X4 93297/62670 (₹ crore)
    assert close(z.value, "7.354855", "0.00001") and z.flag == "safe"
    assert fz.beneish(year, {}).value is None  # one year only: the prior year's lines are missing

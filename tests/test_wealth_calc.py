"""Golden values for every /wealth formula (hand-computed; see each comment) and Monte Carlo reproducibility."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal as D

import numpy as np
import pytest

from finresearch.wealth import allocation, calc, household
from finresearch.wealth.goals import (
    DEFAULT_ASSUMPTIONS,
    ClassAssumption,
    assumptions_from,
    fv_closed_form,
    glide_equity,
    monthly_returns,
    plan,
    required_sip,
    sip_path,
    terminal,
    weights_path,
    wilson,
)


# --------------------------------------------------------------------------- deposits and loans
def test_emi_textbook():
    # ₹10,00,000 at 9 % for 20 years: i = 0.0075, (1.0075)^240 = 6.0092; EMI = P i f / (f - 1) = ₹8,997.26
    assert calc.emi(1_000_000, 9, 240) == D("8997.26")
    assert calc.emi(120_000, 0, 12) == D("10000.00")  # zero rate: principal / months


def test_outstanding_matches_the_schedule_and_ends_at_zero():
    e = calc.emi(1_000_000, 9, 240)
    bal = D(1_000_000)
    for _ in range(12):  # month by month: balance + interest - EMI
        bal = bal + bal * D("0.0075") - e
    assert calc.outstanding(1_000_000, 9, e, 12) == calc.money(bal) == D("981272.89")
    assert calc.outstanding(1_000_000, 9, e, 240) == 0
    assert calc.outstanding(1_000_000, 9, e, 0) == D("1000000.00")


def test_months_to_repay_and_prepayment():
    e = calc.emi(1_000_000, 9, 240)
    assert calc.months_to_repay(1_000_000, 9, e) == pytest.approx(240, abs=1e-3)
    assert calc.months_to_repay(100, 12, 1) == math.inf  # EMI below a month's interest
    b12 = calc.outstanding(1_000_000, 9, e, 12)
    pp = calc.prepayment(b12, 9, e, 100_000)
    assert pp.months_before == pytest.approx(228, abs=1e-3)
    assert pp.months_after < pp.months_before
    # interest left = EMI x months - balance, before and after
    assert pp.interest_before == calc.money(e * D(pp.months_before) - b12)
    assert pp.interest_saved > 0 and float(pp.interest_saved) == pytest.approx(353998.51, abs=1)


def test_fd_compounding():
    # ₹1,00,000 at 7 % compounded quarterly for 365 days: (1 + 0.07/4)^4 = 1.0718590 -> ₹1,07,185.90
    assert calc.fd_value(100_000, 7, date(2025, 1, 1), date(2026, 1, 1)) == D("107185.90")
    # stops at maturity; interest-payout FD is worth its principal; before the start it did not exist
    assert calc.fd_value(100_000, 7, date(2025, 1, 1), date(2027, 1, 1), maturity=date(2026, 1, 1)) == D(
        "107185.90"
    )
    assert calc.fd_value(100_000, 7, date(2025, 1, 1), date(2026, 1, 1), compounding=0) == D("100000.00")
    assert calc.fd_value(100_000, 7, date(2025, 1, 1), date(2024, 1, 1)) == 0


def test_rd_value():
    # 12 monthly instalments of ₹1,000 at 7 %, quarterly compounding: sum_{k=1..12} 1000 (1.0175)^(k/3)
    expect = sum(1000 * 1.0175 ** (k / 3) for k in range(1, 13))
    got = calc.rd_value(1000, 7, date(2025, 1, 1), date(2026, 1, 1), maturity=date(2026, 1, 1))
    assert float(got) == pytest.approx(expect, abs=0.01) and got == D("12462.13")


def test_provident_accrual():
    # EPF-style: ₹1,00,000 at 8.25 % for 12 months, no contributions: interest credited at month 12 = ₹8,250
    assert calc.provident_value(100_000, "8.25", date(2025, 4, 1), date(2026, 4, 1)) == D("108250.00")
    # + ₹1,000 a month: interest = 0.006875 x (100000 x 12 + 1000 x (0 + ... + 11)) = ₹8,703.75
    got = calc.provident_value(100_000, "8.25", date(2025, 4, 1), date(2026, 4, 1), monthly=1000)
    assert got == D("120703.75")
    # six months in: uncredited interest counted pro rata (6 x 687.50)
    assert calc.provident_value(100_000, "8.25", date(2025, 4, 1), date(2025, 10, 1)) == D("104125.00")


def test_months_between():
    assert calc.months_between(date(2025, 1, 31), date(2025, 2, 28)) == 0
    assert calc.months_between(date(2025, 1, 15), date(2025, 3, 15)) == 2
    assert calc.months_between(date(2025, 3, 1), date(2025, 1, 1)) == 0


# --------------------------------------------------------------------------- allocation
def test_glide_rule_of_thumb():
    assert allocation.glide_equity_pct(30) == 70  # 100 - 30
    assert allocation.glide_equity_pct(30, "high") == 80
    assert allocation.glide_equity_pct(15) == 80  # capped at 80
    assert allocation.glide_equity_pct(85, "low") == 10  # floor 20, then -10
    assert allocation.glide_target(30) == {"Equity": 70.0, "Gold": 10.0, "Debt + cash": 20.0}
    assert allocation.glide_target(None) is None
    assert allocation.glide_target(None, equity_override=95) == {
        "Equity": 95.0,
        "Gold": 5.0,
        "Debt + cash": 0.0,
    }


def test_bands_and_compare():
    assert allocation.band_pp(70) == 17.5 and allocation.band_pp(10) == 5
    rows = allocation.compare(
        {"Equity": 50, "Debt": 30, "Cash": 10, "Gold": 10}, {"Equity": 70, "Gold": 10, "Debt + cash": 20}
    )
    by = {r["label"]: r for r in rows}
    assert by["Equity"]["drift_pp"] == -20 and by["Equity"]["outside_band"]
    assert by["Debt + cash"]["weight_pct"] == 40 and by["Debt + cash"]["outside_band"]
    assert not by["Gold"]["outside_band"]


def test_split_asset():
    assert allocation.split_asset("nps", 1000, equity_pct=75) == {"Equity": 750, "Debt": 250}
    assert allocation.split_asset("nps", 1000) == {"Equity": 500, "Debt": 500}
    assert allocation.split_asset("epf", 10) == {"Debt": 10}
    assert allocation.split_asset("other", 10, asset_class="Equity") == {"Equity": 10}


# --------------------------------------------------------------------------- goals Monte Carlo
def test_step_up_closed_form_golden():
    # ₹1,00,000 + ₹10,000/month stepped up 10 % after a year, 24 months at 12 % a year (r_m = 1.12^(1/12) - 1):
    # 100000 x 1.2544 + 10000 x 12.64649 x 1.12 + 11000 x 12.64649 = ₹4,06,192.25
    assert fv_closed_form(100_000, 10_000, 10, 24, 12) == pytest.approx(406_192.25, abs=0.01)


def test_zero_volatility_simulation_equals_the_closed_form():
    flat = {k: ClassAssumption(12.0, 0.0) for k in ("equity", "debt", "gold")}
    w = weights_path(24, 60.0, 10.0)
    r = monthly_returns(24, 3, flat, w, seed=1)
    v = terminal(100_000, sip_path(10_000, 10, 24), r)
    assert np.allclose(v, fv_closed_form(100_000, 10_000, 10, 24, 12), rtol=1e-12)


def test_monte_carlo_is_reproducible_by_seed():
    kw = dict(
        start=200_000,
        sip0=15_000,
        step_up_pct=10,
        months=120,
        target_today=3_000_000,
        inflation_pct=6,
        equity_pct=None,
        gold_pct=10,
        assumptions=DEFAULT_ASSUMPTIONS,
        n=2000,
    )
    a, b = plan(**kw, seed=7), plan(**kw, seed=7)
    assert a.to_json() == b.to_json()
    c = plan(**kw, seed=8)
    assert c.p_success != a.p_success or c.terminal_pcts != a.terminal_pcts
    # target in rupees of the goal date: 30 lakh x 1.06^10
    assert a.target_nominal == pytest.approx(3_000_000 * 1.06**10)
    lo, hi = a.p_ci
    assert lo <= a.p_success <= hi
    assert a.p_haircut <= a.p_success  # lower equity returns on the same draws cannot help
    assert a.p_step_up_plus5 >= a.p_success
    assert a.sip_for_75 is not None and a.sip_for_90 is not None and a.sip_for_90 >= a.sip_for_75


def test_required_sip_bisection_hits_the_share():
    flat = {k: ClassAssumption(0.0, 0.0) for k in ("equity", "debt", "gold")}
    r = monthly_returns(12, 10, flat, weights_path(12, 0, 0), seed=3)
    # zero return: 12 x SIP >= 1,20,000 -> SIP ₹10,000
    assert required_sip(0, 0, 120_000, r, 0.75) == 10_000
    assert required_sip(200_000, 0, 120_000, r, 0.75) == 0.0


def test_glide_and_wilson():
    assert [glide_equity(y) for y in (10, 5, 2, 0.5)] == [70, 50, 20, 0]
    lo, hi = wilson(50, 100)
    assert (round(lo, 4), round(hi, 4)) == (0.4038, 0.5962)
    assert wilson(0, 0) == (0.0, 1.0)


def test_assumption_validation():
    assert assumptions_from({"equity": {"mu_pct": 10, "sigma_pct": 15}})["equity"].mu_pct == 10
    with pytest.raises(ValueError):
        assumptions_from({"equity": {"mu_pct": 90, "sigma_pct": 15}})


# --------------------------------------------------------------------------- household
def test_emergency_rules():
    assert household.emergency_target(None, 0, 2)[0] == 6
    assert household.emergency_target(None, 2, 2)[0] == 9
    assert household.emergency_target(None, 0, 1)[0] == 9
    assert household.emergency_target(None, 1, 1)[0] == 12
    assert household.emergency_target(4, 3, 1) == (4.0, "your target")
    assert household.emergency_months(300_000, 50_000) == 6
    assert household.emergency_months(300_000, None) is None


def test_dicgc_concentration_per_bank():
    rows = household.deposit_concentration(
        [("HDFC Bank", 400_000), ("hdfc bank ltd.", 200_000), ("SBI", 500_000), (None, 900_000)]
    )
    by = {r["bank"]: r for r in rows}
    assert (
        by["HDFC Bank"]["total"] == 600_000
        and by["HDFC Bank"]["over_limit"]
        and by["HDFC Bank"]["uninsured"] == 100_000
    )
    assert not by["SBI"]["over_limit"]  # exactly ₹5 lakh is covered
    assert not by["(no bank named)"]["over_limit"]


def test_term_cover_needs_method():
    # real rate (1.07 / 1.06) - 1 = 0.943396 %; PV of ₹6,00,000 a year for 20 years, paid at the start of each year
    r = 1.07 / 1.06 - 1
    pv = 600_000 * (1 - (1 + r) ** -20) / r * (1 + r)
    assert pv == pytest.approx(10_992_019, abs=1)  # ≈ 18.32 years of expenses
    need = household.term_cover(
        annual_expenses=600_000,
        years=20,
        nominal_pct=7,
        inflation_pct=6,
        loans=2_000_000,
        goal_gaps=1_000_000,
        assets=3_000_000,
        existing=5_000_000,
    )
    assert need.expenses_pv == pytest.approx(10_992_019, abs=1)
    assert need.need == pytest.approx(pv + 3_000_000)
    assert need.gap == pytest.approx(pv + 3_000_000 - 8_000_000)
    assert household.pv_annuity_due(100, 3, 0) == 300


def test_foir_and_deductions():
    assert household.foir_pct(20_000, 100_000) == 20
    assert household.foir_pct(20_000, None) is None
    # ₹30 lakh home loan at 9 % -> ₹2.7 lakh interest; 2/2.7 of it deductible (old regime); slab 30 %:
    # 9 x (1 - 0.3 x 200000/270000) = 7.00 %
    out = household.prepay_vs_invest(
        kind="home",
        rate_pct=9,
        balance=3_000_000,
        regime="old",
        slab_pct=30,
        equity_mu_pct=11,
        equity_sigma_pct=17,
        debt_mu_pct=7,
        years_left=15,
    )
    assert out["after_tax_loan_pct"] == pytest.approx(7.0)
    assert out["debt_post_tax_pct"] == pytest.approx(4.9) and out["equity_post_tax_pct"] == pytest.approx(
        9.625
    )
    assert household.deductible_share("home", "new", 270_000)[0] == 0
    assert household.deductible_share("personal", "old", 1)[0] == 0
    pers = household.prepay_vs_invest(
        kind="personal",
        rate_pct=14,
        balance=100_000,
        regime="new",
        slab_pct=30,
        equity_mu_pct=11,
        equity_sigma_pct=17,
        debt_mu_pct=7,
        years_left=2,
    )
    assert pers["after_tax_loan_pct"] == 14 and "certain" in pers["reading"]


def test_p_equity_beats_loan_rate():
    # m = ln 1.11 - 0.17^2/2 = 0.089910; (m - ln 1.08) sqrt(10) / 0.17 = 0.2409 -> Phi = 0.5952
    assert household.p_equity_beats(8, 11, 17, 10) == pytest.approx(0.5952, abs=5e-4)
    assert household.p_equity_beats(8, 11, 17, 0) is None

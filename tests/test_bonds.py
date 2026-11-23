"""Listed bonds: bond arithmetic (golden values), NSE bond list parsing and warnings, analytics, and the bond kind."""

from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.adapters.nse_bonds import parse_live_bonds, search_bonds
from finresearch.agents.roles import BOND_STREAMS
from finresearch.fincalc import bonds as b

FIX = Path(__file__).parent / "fixtures" / "nse" / "bonds_live_trimmed.json"


def listed():
    return parse_live_bonds(json.loads(FIX.read_text()))


def test_bond_arithmetic_golden_values():
    # a par bond with equal coupons on a coupon date yields exactly its coupon: a half-yearly G-sec, and a listed
    # bond paying once a year (#519: a listed bond's half-yearly coupons follow the days of each half, so that case
    # moved to the G-sec rule; test_listed_bond_coupons_are_paid_for_the_actual_days_of_each_period has its prices)
    assert b.clean_price("0.08", date(2026, 1, 1), date(2031, 1, 1), "0.08", 2, day_count=b.GSEC) == pytest.approx(
        Decimal(100), abs=Decimal("1e-9")
    )
    assert b.ytm(100, date(2026, 1, 1), date(2031, 1, 1), "0.08", 2, day_count=b.GSEC) == Decimal("0.08000000")
    assert b.clean_price("0.08", date(2026, 1, 1), date(2031, 1, 1), "0.08", 1) == pytest.approx(
        Decimal(100), abs=Decimal("1e-9")
    )
    assert b.ytm(100, date(2026, 1, 1), date(2031, 1, 1), "0.08", 1) == Decimal("0.08000000")
    # textbook: 5-year 10% annual coupon priced at 96.304 yields 11%
    assert round(b.ytm("96.304", date(2026, 1, 1), date(2031, 1, 1), "0.10", 1), 4) == Decimal("0.1100")
    # a 5-year zero-coupon bond's Macaulay duration is 5 years
    assert b.duration("0.07", date(2026, 1, 1), date(2031, 1, 1), 0, 1).macaulay == Decimal("5.0")
    # accrued: 8.75% on 1,000 for 235 days (5-Feb to 28-Sep) = 56.3356...
    assert round(b.accrued_interest(date(2026, 9, 28), date(2029, 2, 5), "0.0875", 1, 1000), 4) == Decimal(
        "56.3356"
    )
    assert b.coupon_dates(date(2026, 9, 28), date(2029, 2, 5), 1) == [
        date(2027, 2, 5),
        date(2028, 2, 5),
        date(2029, 2, 5),
    ]
    assert b.coupon_dates(date(2026, 9, 28), date(2027, 1, 31), 12)[0] == date(2026, 9, 30)  # month-end clamp
    assert b.post_tax_yield("0.09", "0.312") == Decimal("0.061920")
    assert b.current_yield(1123.99, "0.0875", 1000).quantize(Decimal("0.0001")) == Decimal("0.0778")
    with pytest.raises(ValueError):
        b.coupon_dates(date(2026, 1, 1), date(2025, 1, 1), 1)


def test_nse_bond_list_parse_search_and_warnings():
    bonds = listed()
    nhai = search_bonds(bonds, "875NHAI29")[0]
    assert (nhai.isin, nhai.coupon_pct, nhai.face_value, nhai.maturity) == (
        "INE906B07DF8", Decimal("8.75"), Decimal("1000"), date(2029, 2, 5))  # fmt: skip
    assert nhai.as_of.date() == date(2026, 9, 28)
    scl = next(x for x in bonds if x.isin == "INE148I07RB1")
    assert scl.rating == "AA/Stable" and scl.rating_agency == "CRISIL"
    assert any("partly redeemed" in w for w in scl.warnings)  # face value 800
    assert any("in the past" in w for w in scl.warnings)  # next interest date 2023
    assert search_bonds(bonds, "INE906B07DF8")[0].symbol == "875NHAI29" and search_bonds(bonds, "") == []


async def test_bond_analytics_requires_terms_and_refuses_partly_redeemed_bonds(monkeypatch):
    from finresearch.adapters import nse_bonds
    from finresearch.mcp_server.server import bond_analytics

    async def fake_live(client=None):
        return listed()

    monkeypatch.setattr(nse_bonds, "live_bonds", fake_live)
    out = json.loads(
        await bond_analytics("INE906B07DF8", 1, "dirty", settlement="2026-09-28", tax_rate="0.312")
    )
    assert (
        out["accrued_interest"] == "56.3356"
        and out["clean_price"] == "1067.6544"
        and out["price_basis"] == "dirty"
    )
    assert Decimal("0.05") < Decimal(out["ytm"]) < Decimal("0.06") and "after_tax_ytm" in out
    assert "error" in json.loads(await bond_analytics("INE906B07DF8", 1, "cum"))
    assert "partly redeemed" in json.loads(await bond_analytics("INE148I07RB1", 12, "dirty"))["error"]
    assert "not in NSE" in json.loads(await bond_analytics("INE000000000", 1, "dirty"))["error"]


async def test_bond_pipeline_records_listing_facts_and_runs_bond_roles(env, tmp_path, monkeypatch):
    from finresearch.adapters import nse_bonds
    from finresearch.agents.schemas import (
        BondSynthesis,
        CaseReport,
        ClaimVerdict,
        CriticReport,
        ResearchPlan,
        StreamFocus,
        StreamReport,
        VerificationReport,
    )
    from finresearch.bridge.limits import LimitTracker
    from finresearch.bridge.types import AgentResult, RateLimitSnapshot, Tier
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.orchestrator.base import PipelineConfig, create_run
    from finresearch.orchestrator.bond import ensure_bond_company
    from finresearch.orchestrator.kinds import pipeline_for

    async def fake_live(client=None):
        return listed()

    monkeypatch.setattr(nse_bonds, "live_bonds", fake_live)
    made = await ensure_bond_company("INE906B07DF8", bonds=fake_live)
    assert made["slug"] == "bond-ine906b07df8"
    run_id = create_run(made["slug"], kind="bond_report")
    calls = []

    async def runner(role, ctx, **extra):
        calls.append(role)
        if role == "bond_planner":
            assert ctx.nse_symbol == "INE906B07DF8"
            out = ResearchPlan(company_one_liner="x", critical_questions=["q"],
                               streams=[StreamFocus(stream=s, questions=["?"]) for s in BOND_STREAMS])  # fmt: skip
        elif role in BOND_STREAMS:
            with session_scope() as s:
                c = Claim(run_id=run_id, stream=role, statement=f"{role} fact", claim_type="factual")
                s.add(c)
                s.flush()
                cid = c.id
            out = StreamReport(
                summary=role, section_markdown=f"{role} [C{cid}]", key_findings=[], claim_ids=[cid]
            )
        elif role == "verifier":
            ids = [c["claim_id"] for c in json.loads(extra["claims"])]
            out = VerificationReport(verdicts=[ClaimVerdict(claim_id=i, verdict="verified", evidence="ok") for i in ids],
                                     summary="ok")  # fmt: skip
        elif role in ("bond_bull", "bond_bear"):
            out = CaseReport(
                thesis=role, points=[], listing_view="-", long_term_view="-", strongest_counterargument="-"
            )
        elif role == "bond_synthesizer":
            out = BondSynthesis(verdict="HOLD", suits="-", confidence="low", executive_summary="-", reasons_for=[],
                                reasons_against=[], action_checklist=[], report_markdown="# NHAI 2029")  # fmt: skip
        else:
            out = CriticReport(gaps=[], ready_to_publish=True)
        return out, AgentResult(task_name=role, tier=Tier.CLAUDE_MAX, model="fake", ok=True, structured_output={},
                                rate_limit=RateLimitSnapshot(five_hour_utilization=0.1,
                                                             five_hour_resets_at=int(time.time()) + 3600))  # fmt: skip

    pipe = pipeline_for(run_id, runner=runner, tracker=LimitTracker(tmp_path / "limits"),
                        config=PipelineConfig(concurrency=3, render=False))  # fmt: skip
    assert await pipe.run() == "done" and set(BOND_STREAMS) <= set(calls) and "bond_synthesizer" in calls
    with session_scope() as s:
        facts = {c.metric: c.value for c in s.query(Claim).filter_by(run_id=run_id, stream="facts")}
    assert facts == {"coupon_rate": Decimal("8.75"), "face_value": Decimal("1000"), "last_price": Decimal("1123.99"),
                     "maturity_year": Decimal("2029")}  # fmt: skip


def test_after_tax_ytm_uses_after_tax_cash_flows():
    """Live bond run 12: YTM x (1 - t) overstated the post-tax yield of a bond bought at a premium."""
    s, m = date(2026, 1, 1), date(2031, 1, 1)
    # at par, taxing coupons at 30% gives exactly 70% of the yield
    assert b.after_tax_ytm(100, s, m, "0.10", 1, "0.30") == pytest.approx(
        Decimal("0.07"), abs=Decimal("1e-6")
    )
    # bought at a premium: the pull to par is an unrelieved capital loss, so the after-tax yield is below YTM x 0.7
    premium_ytm = b.ytm(105, s, m, "0.10", 1)
    after = b.after_tax_ytm(105, s, m, "0.10", 1, "0.30")
    assert after < premium_ytm * Decimal("0.7")
    # with the loss offset against other gains it is higher than without
    assert b.after_tax_ytm(105, s, m, "0.10", 1, "0.30", loss_offset=True) > after
    # at a discount the gain is taxed too
    assert b.after_tax_ytm(95, s, m, "0.10", 1, "0.30") < b.ytm(95, s, m, "0.10", 1) * Decimal(
        "0.7"
    ) + Decimal("0.01")


def test_accrued_interest_is_actual_actual_per_sebi():
    """SEBI CIR/IMD/DF-1/122/2016: 366 days is the denominator for the one-year period containing 29 February."""
    m = date(2029, 3, 13)  # L&T Finance 8.98% NCD, monthly on the 13th
    # 13-Feb-2028 to 10-Mar-2028: 26 days in the year 13-Mar-2027..13-Mar-2028, which contains 29-Feb-2028
    leap = b.accrued_interest(date(2028, 3, 10), m, "0.0898", 12, 1000)
    assert leap == pytest.approx(Decimal(1000) * Decimal("0.0898") * 26 / 366, abs=Decimal("1e-9"))
    # 13-Sep-2026 to 29-Sep-2026: 16 days in a year without 29-February
    normal = b.accrued_interest(date(2026, 9, 29), m, "0.0898", 12, 1000)
    assert normal == pytest.approx(Decimal(1000) * Decimal("0.0898") * 16 / 365, abs=Decimal("1e-9"))
    # annual coupons: 13-Mar-2027 to 10-Mar-2028 is 363 days in the year that contains 29-Feb-2028
    annual = b.accrued_interest(date(2028, 3, 10), m, "0.0898", 1, 1000)
    assert annual == pytest.approx(Decimal(1000) * Decimal("0.0898") * 363 / 366, abs=Decimal("1e-9"))


def test_ytm_raises_when_no_yield_in_range_fits():
    """Bisection used to return its bound: a price of 10 on a 9 % bond read as a 100.000000 % YTM, and an after-tax
    yield likewise. Both now raise; a par price on a coupon date still yields the coupon exactly."""
    s, m = date(2026, 10, 1), date(2030, 4, 1)
    with pytest.raises(ValueError, match="no yield"):
        b.ytm(10, s, m, "0.09", 1)
    with pytest.raises(ValueError, match="no yield"):
        b.ytm(100000, s, m, "0.09", 1)  # above the price at -50 %
    with pytest.raises(ValueError, match="no after-tax yield"):
        b.after_tax_ytm(10, s, m, "0.09", 1, "0.3")
    assert b.ytm(100, date(2026, 4, 1), m, "0.09", 1) == Decimal("0.09")
    assert b.ytm(100, date(2026, 4, 1), m, "0.09", 2, day_count=b.GSEC) == Decimal("0.09")  # equal coupons (#519)


def test_month_end_maturity_keeps_its_coupon_day_for_the_previous_coupon():
    """#503 (found by the QuantLib oracle): a bond maturing on the 29th, 30th or 31st pays in a shorter month on that
    month's last day (31-Aug -> 28-Feb), and the coupon before a clamped date is the maturity day again (31-Aug), not
    the clamped day carried back (28-Aug). `previous_coupon` and `cash_flows` stepped back from the clamped date, so
    accrued interest gained up to three days and the first discounting fraction used a period three days too long.
    Expected values by hand, from the schedule and a calendar."""
    aug31 = date(2030, 8, 31)  # half-yearly: 28/29-Feb and 31-Aug
    assert b.coupon_dates(date(2026, 1, 1), aug31, 2)[:2] == [date(2026, 2, 28), date(2026, 8, 31)]
    assert b.previous_coupon(date(2026, 1, 1), aug31, 2) == date(2025, 8, 31)  # was 28-Aug-2025
    # 31-Aug-2025 to 1-Jan-2026 is 30 + 31 + 30 + 31 + 1 = 123 days, in a year without 29 February (was 126 days)
    assert b.accrued_interest(date(2026, 1, 1), aug31, "0.09", 2, 1000) == pytest.approx(
        Decimal(90) * 123 / 365, abs=Decimal("1e-9")
    )
    # on a coupon date nothing has accrued and a par bond is worth par (was 3 days' interest and 99.99...)
    assert b.accrued_interest(date(2025, 8, 31), aug31, "0.1425", 2) == 0
    # (#519: with equal coupons, the G-sec rule; a listed bond's two halves pay for 181 and 184 days, below)
    assert b.clean_price("0.09", date(2025, 8, 31), aug31, "0.09", 2, day_count=b.GSEC) == pytest.approx(
        Decimal(100), abs=Decimal("1e-9")
    )
    assert b.ytm(100, date(2025, 8, 31), aug31, "0.09", 2, day_count=b.GSEC) == Decimal("0.09")
    # the listed bond a year from maturity, on its coupon date 31-Aug-2029: 181 days to 28-Feb-2030 and 184 to
    # 31-Aug-2030 in a coupon year without 29 February, discounted one and two whole half-years
    last_year = 9 * 181 / 365 / 1.045 + (100 + 9 * 184 / 365) / 1.045**2
    assert float(b.clean_price("0.09", date(2029, 8, 31), aug31, "0.09", 2)) == pytest.approx(last_year, abs=1e-9)
    # a zero maturing 31-Aug-2026, seen on 1-Jan-2026: 58 of the 181 days of the period 31-Aug-2025..28-Feb-2026 are
    # left, then one whole period, so it is discounted over 1 + 58/181 half-years (was 1 + 58/184)
    periods = 1 + 58 / 181
    flows = b.cash_flows(date(2026, 1, 1), date(2026, 8, 31), 0, 2)
    assert [float(cf.periods) for cf in flows] == pytest.approx([58 / 181, periods], abs=1e-12)
    assert float(b.dirty_price("0.10", date(2026, 1, 1), date(2026, 8, 31), 0, 2)) == pytest.approx(
        100 / 1.05**periods, abs=1e-9
    )
    assert b.duration("0.10", date(2026, 1, 1), date(2026, 8, 31), 0, 2).macaulay == Decimal(
        str(round(periods / 2, 6))
    )
    # the commonest Indian case, a 31-March maturity paying 30-Sep and 31-Mar: 31-Mar to 15-Jun-2026 is
    # 30 + 31 + 15 = 76 days (was 77, counted from 30-Mar)
    mar31 = date(2030, 3, 31)
    assert b.previous_coupon(date(2026, 6, 15), mar31, 2) == date(2026, 3, 31)
    assert b.accrued_interest(date(2026, 6, 15), mar31, "0.08", 2) == pytest.approx(
        Decimal(8) * 76 / 365, abs=Decimal("1e-9")
    )
    # monthly on the 31st: the coupon before 30-Apr-2026 is 31-Mar-2026, 16 days before 16-Apr (was 30-Mar: 17)
    assert b.previous_coupon(date(2026, 4, 16), date(2027, 1, 31), 12) == date(2026, 3, 31)
    assert b.accrued_interest(date(2026, 4, 16), date(2027, 1, 31), "0.12", 12, 1000) == pytest.approx(
        Decimal(120) * 16 / 365, abs=Decimal("1e-9")
    )
    # a maturity every month has is unchanged
    assert b.previous_coupon(date(2026, 9, 28), date(2029, 2, 5), 1) == date(2026, 2, 5)


def test_listed_bond_coupons_are_paid_for_the_actual_days_of_each_period():
    """#519. `cash_flows` paid face x coupon / freq every period while `accrued_interest` followed SEBI's day count,
    so a half-yearly, quarterly or monthly bond accrued one amount and paid another. SEBI's rule is one rule for
    both. CIR/IMD/DF-1/122/2016, clause 3(b), for bonds issued 1-Jan-2016 paying half-yearly on 1-Jul and 1-Jan:
    "366 days would be reckoned as the denominator (Actual/Actual), for payment of interest, in both the half year
    periods i.e. Jan 01, 2016 to Jul 01, 2016 and Jul 01, 2016 to Jan 01, 2017". Those halves have 182 and 184 days
    (31+29+31+30+31+30 and 31+31+30+31+30+31), so 1,000 at 9 % pays 90 x 182/366 and 90 x 184/366, not 45 and 45;
    the next coupon year holds no 29 February: 90 x 181/365 and 90 x 184/365. Expected values by hand."""
    s, m = date(2016, 1, 1), date(2018, 1, 1)
    flows = b.cash_flows(s, m, "0.09", 2, 1000)
    assert [cf.day for cf in flows] == [date(2016, 7, 1), date(2017, 1, 1), date(2017, 7, 1), date(2018, 1, 1)]
    want = [Decimal(90) * 182 / 366, Decimal(90) * 184 / 366, Decimal(90) * 181 / 365, Decimal(90) * 184 / 365]
    assert [cf.coupon for cf in flows] == pytest.approx(want, abs=Decimal("1e-12"))
    assert [round(cf.coupon, 4) for cf in flows] == [
        Decimal("44.7541"), Decimal("45.2459"), Decimal("44.6301"), Decimal("45.3699")]  # fmt: skip
    assert [cf.amount for cf in flows[:3]] == [cf.coupon for cf in flows[:3]]
    assert flows[3].amount == flows[3].coupon + 1000  # the principal comes with the last coupon
    # the periods of one coupon year add up to the year's coupon, leap year or not
    assert flows[0].coupon + flows[1].coupon == pytest.approx(Decimal(90), abs=Decimal("1e-12"))
    assert flows[2].coupon + flows[3].coupon == pytest.approx(Decimal(90), abs=Decimal("1e-12"))
    # accrual and payment are the same sum: the eve of a coupon has accrued all of it but that one day's interest
    assert b.accrued_interest(date(2016, 6, 30), m, "0.09", 2, 1000) + Decimal(90) / 366 == pytest.approx(
        want[0], abs=Decimal("1e-12")
    )
    assert b.accrued_interest(date(2017, 12, 31), m, "0.09", 2, 1000) + Decimal(90) / 365 == pytest.approx(
        want[3], abs=Decimal("1e-12")
    )
    # two periods left on 1-Jan-2017: the price is the two flows discounted one and two half-years (closed form)
    c1, c2 = 90 * 181 / 365, 90 * 184 / 365
    price = c1 / 1.045 + (c2 + 1000) / 1.045**2
    assert float(b.dirty_price("0.09", date(2017, 1, 1), m, "0.09", 2, 1000)) == pytest.approx(price, abs=1e-9)
    assert b.ytm(Decimal(str(price)), date(2017, 1, 1), m, "0.09", 2, 1000) == Decimal("0.09")
    # quarterly and monthly, 12 % on 1,000 maturing 15-Jan-2028, in the coupon year 15-Jan-2026..15-Jan-2027 (365):
    # 15-Jan to 15-Apr is 16 + 28 + 31 + 15 = 90 days; 15-Feb to 15-Mar is 28 days, 15-Mar to 15-Apr is 31
    q = b.cash_flows(date(2026, 1, 15), date(2028, 1, 15), "0.12", 4, 1000)
    assert q[0].day == date(2026, 4, 15) and q[0].coupon == pytest.approx(Decimal(120) * 90 / 365, abs=Decimal("1e-12"))
    mth = b.cash_flows(date(2026, 2, 15), date(2028, 1, 15), "0.12", 12, 1000)
    assert [cf.coupon for cf in mth[:2]] == pytest.approx(
        [Decimal(120) * 28 / 365, Decimal(120) * 31 / 365], abs=Decimal("1e-12")
    )
    assert sum(cf.coupon for cf in b.cash_flows(date(2026, 1, 15), date(2027, 1, 15), "0.12", 12, 1000)) == (
        pytest.approx(Decimal(120), abs=Decimal("1e-12"))
    )
    # annual coupons were already right: the period is the year, 365/365 or 366/366
    assert {cf.coupon for cf in b.cash_flows(date(2026, 1, 1), date(2031, 1, 1), "0.08", 1)} == {Decimal(8)}


def test_sebis_own_illustrations_of_cash_flows():
    """#519, the tables printed by SEBI, hand-checked. Master circular SEBI/HO/DDHS/DDHS-PoD/P/CIR/2025/0000000137
    (15-Oct-2025), Chapter III, Table 1: XYZ Limited, face 10,00,000, allotted 14-Dec-2020, redeemed 14-Dec-2025,
    8.95 % a year paid annually; "Number of days for denominator" 365, 365, 365, 366, 365; every coupon 89,500;
    total 14,47,500. The 366 belongs to the coupon year 14-Dec-2023..14-Dec-2024 (it holds 29-Feb-2024); the year
    from 14-Dec-2024 has 365 although it starts in calendar 2024. The table is annual, so it passed before #519
    too: it pins the denominator's year and the holiday rule, not the unequal coupons."""
    s, m = date(2020, 12, 14), date(2025, 12, 14)
    flows = b.cash_flows(s, m, "0.0895", 1, 1000000)
    assert [cf.day for cf in flows] == [date(y, 12, 14) for y in range(2021, 2026)]
    assert [cf.coupon for cf in flows] == [89500] * 5 and flows[-1].amount == 1089500
    assert sum(cf.amount for cf in flows) == 1447500
    # the denominator of each coupon year, read off the accrual of its first day
    first_day = [b.accrued_interest(date(y, 12, 15), m, "0.0895", 1, 1000000) for y in range(2020, 2025)]
    assert [round(Decimal(89500) / x) for x in first_day] == [365, 365, 365, 366, 365]
    # "the calculation for payment of interest shall be only till December 13, 2024": the 4th coupon is due on
    # Saturday 14-Dec-2024 and paid on Monday the 16th, and the amount does not grow with the two days
    assert b.interest(date(2023, 12, 14), date(2024, 12, 14), m, "0.0895", 1000000) == 89500
    assert b.accrued_interest(date(2024, 12, 13), m, "0.0895", 1, 1000000) == pytest.approx(
        Decimal(89500) * 365 / 366, abs=Decimal("1e-9")
    )
    # CIR/IMD/DF/18/2013, Annexure A (allotted 13-Nov-2013, redeemed 13-Nov-2018, same face and rate): the rule
    # BEFORE the 2016 clarification paid the extra day when a holiday moved the payment: 367 days to Monday
    # 14-Nov-2016 = 89,745 and the 364 days after it = 89,255 (89,500 x 364/365 = 89,254.79). The rupee amounts
    # agree; the paise differ by which year the 367th day is counted in (89,744.54 printed as 89,745, here
    # 89,500 + 89,500/365 = 89,745.21), and a bond issued from 1-Jan-2017 pays 89,500 both times.
    m13 = date(2018, 11, 13)
    assert round(b.interest(date(2015, 11, 13), date(2016, 11, 14), m13, "0.0895", 1000000)) == 89745
    assert round(b.interest(date(2016, 11, 14), date(2017, 11, 13), m13, "0.0895", 1000000)) == 89255
    assert [cf.coupon for cf in b.cash_flows(date(2013, 11, 13), m13, "0.0895", 1, 1000000)] == [89500] * 5
    # an issuer's schedule in numbers (Srei Infrastructure Finance, Tranche 1 Prospectus of 5-Feb-2018 on
    # sebi.gov.in, Series III: 8.43 % monthly on 1,000, redeemed 20-Mar-2021): 31 and 30 days pay 7.00, the 28 days
    # to 7-Mar-2019 pay 6.00, the 48 days of the first period 11.00. Equal coupons would be 7.025 every month.
    srei = date(2021, 3, 20)
    paid = [(date(2018, 5, 7), date(2018, 6, 7), 7), (date(2018, 6, 7), date(2018, 7, 7), 7),
            (date(2019, 2, 7), date(2019, 3, 7), 6), (date(2018, 3, 20), date(2018, 5, 7), 11)]  # fmt: skip
    assert [round(b.interest(a, z, srei, "0.0843", 1000)) for a, z, _ in paid] == [x for _, _, x in paid]


def test_government_securities_pay_equal_half_yearly_coupons_and_accrue_30_360():
    """#519: the other instrument class. RBI, "Government Securities Market in India - A Primer"
    (https://www.rbi.org.in/Scripts/FAQView.aspx?Id=79, read 23-Nov-2026): "8.24%GS2018 was issued on April 22, 2008
    for a tenor of 10 years maturing on April 22, 2018. Coupon on this security will be paid half-yearly at 4.12%
    (half yearly payment being half of the annual coupon of 8.24%) of the face value on October 22 and April 22 of
    each year"; "Bond market: The day count convention followed is 30/360"; and its worked accrual: security 8.83%
    2023, last coupon 25-Nov-2013, settlement 30-Jan-2014, 65 days, "8.83 x (65/360) = Rs 1.5943"."""
    flows = b.cash_flows(date(2008, 4, 22), date(2018, 4, 22), "0.0824", 2, day_count=b.GSEC)
    assert len(flows) == 20 and {cf.coupon for cf in flows} == {Decimal("4.12")}
    assert {(cf.day.month, cf.day.day) for cf in flows} == {(10, 22), (4, 22)} and flows[-1].amount == Decimal("104.12")
    ai = b.accrued_interest(date(2014, 1, 30), date(2023, 11, 25), "0.0883", 2, day_count=b.GSEC)
    assert ai == Decimal("8.83") * 65 / 360 and round(ai, 4) == Decimal("1.5943")
    # the same dates under the listed-bond rule are 66 actual days over 365: the classes must not be mixed
    assert b.accrued_interest(date(2014, 1, 30), date(2023, 11, 25), "0.0883", 2) == pytest.approx(
        Decimal("8.83") * 66 / 365, abs=Decimal("1e-12")
    )
    # 31sts count as 30ths: 31-Jan to 31-Mar is 60 days, not 59
    assert b.accrued_interest(date(2026, 3, 31), date(2030, 7, 31), "0.072", 2, day_count=b.GSEC) == Decimal(
        "7.2") * 60 / 360  # fmt: skip
    with pytest.raises(ValueError, match="day_count"):
        b.cash_flows(date(2026, 1, 1), date(2031, 1, 1), "0.08", 2, day_count="actual/360")


def test_a_zero_yield_is_zero_not_minus_zero():
    """#503: at a zero yield a bond is worth the plain sum of its cash flows (5 x 8 + 100 = 140). The bisection ends
    a hair below zero and the rounding kept the sign: the yield read Decimal('-0E-8') and printed "-0.00%"."""
    s, m = date(2026, 1, 1), date(2031, 1, 1)
    y = b.ytm(140, s, m, "0.08", 1)
    assert y == 0 and not y.is_signed() and f"{y:.2%}" == "0.00%"
    a = b.after_tax_ytm(140, s, m, "0.08", 1, 0)
    assert a == 0 and not a.is_signed() and f"{a:.2%}" == "0.00%"
    assert b.ytm(141, s, m, "0.08", 1) < 0  # a real negative yield keeps its sign

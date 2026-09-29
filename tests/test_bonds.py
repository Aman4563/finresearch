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
    # a par bond on a coupon date yields exactly its coupon
    assert b.clean_price("0.08", date(2026, 1, 1), date(2031, 1, 1), "0.08", 2) == pytest.approx(
        Decimal(100), abs=Decimal("1e-9")
    )
    assert b.ytm(100, date(2026, 1, 1), date(2031, 1, 1), "0.08", 2) == Decimal("0.08000000")
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

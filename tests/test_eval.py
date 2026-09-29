"""Gold-set scorer: unit normalisation, period matching, contradictions in the report, verdict agreement."""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from finresearch.evals.gold import GOLD_DIR, evaluate, load_gold

GOLD = {
    "company": "t",
    "verdict": {"listing": "APPLY", "long_term": ["AVOID", "NEUTRAL"]},
    "facts": [
        {"id": "pat", "label": "PAT FY26", "patterns": [r"\bpat\b|profit"], "period": r"fy ?2026|fy26",
         "value": 535.61, "unit": "INR million", "tolerance": 0.005, "importance": "high"},
        {"id": "mcap", "label": "Market cap", "patterns": ["market cap"], "period": None, "value": 30953.5,
         "unit": "INR million", "tolerance": 0.005, "importance": "high"},
        {"id": "cust", "label": "Largest customer", "patterns": ["largest customer"], "period": None, "value": 38.54,
         "unit": "%", "tolerance": 0.005, "importance": "normal"},
        {"id": "lot", "label": "Lot", "patterns": [r"\blot\b"], "period": None, "value": 55, "unit": "shares",
         "tolerance": 0.0, "importance": "normal"},
    ],
}  # fmt: skip


@pytest.fixture
def run(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "eval-" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:8], "E")
        r = ResearchRun(company_id=co.id, kind="ipo_report", manifest={"final_gate": {"ok": True}})
        s.add(r)
        s.flush()

        def add(**kw):
            c = Claim(run_id=r.id, claim_type="numeric", **kw)
            s.add(c)
            s.flush()
            return c.id

        ids = {
            "pat_cr": add(stream="financials", statement="PAT FY26", metric="pat", value=Decimal("53.561"),
                          unit="INR crore", period="FY2026", status="verified"),
            "pat_wrong_year": add(stream="financials", statement="PAT", metric="pat", value=Decimal("535.61"),
                                  unit="INR million", period="FY2025", status="verified"),
            "mcap_bad": add(stream="valuation", statement="Post-issue market cap", metric="market_cap",
                            value=Decimal("3200"), unit="INR crore", period="post-issue", status="unverified"),
            "cust_frac": add(stream="business", statement="Largest customer share", metric="largest_customer_share",
                             value=Decimal("0.3854"), unit="fraction", period="Q1 FY27", status="needs_review"),
            "lot_contra": add(stream="valuation", statement="lot size", metric="lot", value=Decimal("55"),
                              unit="shares", period="-", status="contradicted"),
        }  # fmt: skip
        report = f"PAT ₹53.56 cr [C{ids['pat_cr']}]; market cap ₹3,200 cr [C{ids['mcap_bad']}]."
        s.add(AgentStep(run_id=r.id, key="synthesis", stage="synthesis", role="synthesizer", status="done",
                        num_turns=10, duration_s=120, five_hour_before=0.1, five_hour_after=0.14,
                        output={"report_markdown": report, "overall_verdict": "APPLY-CONDITIONAL",
                                "verdict_listing": "APPLY-CONDITIONAL", "verdict_long_term": "AVOID at 272"}))  # fmt: skip
        return r.id, ids, report


def test_scoring_units_periods_contradictions_and_verdict(run):
    from finresearch.db import session_scope

    run_id, ids, report = run
    with session_scope() as s:
        res = evaluate(s, run_id, GOLD, report)
    by = {f.id: f for f in res.facts}
    assert (
        by["pat"].found and by["pat"].verified and by["pat"].claim_ids == [ids["pat_cr"]]
    )  # crore -> million
    assert not by["mcap"].found and by["mcap"].contradicted_in_report == [ids["mcap_bad"]]
    assert by["cust"].found and not by["cust"].verified  # fraction -> %, needs_review still counts as found
    assert not by["lot"].found  # contradicted claims never count
    assert res.recall == 0.5 and res.high_recall == 0.5 and len(res.contradicted) == 1
    assert res.verdict_agrees and not res.passes_release_bar
    assert abs(res.stats["five_hour_used"] - 0.04) < 1e-9 and res.stats["steps"] == 1
    md = res.markdown()
    assert "key-fact recall | **50%** (2/4)" in md and "| release bar" in md and "FAIL" in md


@pytest.mark.parametrize("company", ["moneyview", "orient-cables", "infosys", "mf-120505"])
def test_gold_files_are_well_formed(company):
    import re

    gold = load_gold(company)
    # IPO and stock gold sets come from full manual fact checks; fund sets use AMFI primary data only
    minimum = 8 if gold.get("kind") == "fund_report" else 20
    assert gold["company"] == company and len(gold["facts"]) >= minimum
    assert (GOLD_DIR / f"{company}.json").exists()
    ids = [f["id"] for f in gold["facts"]]
    assert len(ids) == len(set(ids))
    for f in gold["facts"]:
        for p in f["patterns"] + ([f["period"]] if f.get("period") else []):
            re.compile(p)
        assert f["unit"] in {"INR million", "INR crore", "INR", "%", "shares"} and f["importance"] in {
            "high",
            "normal",
        }
        assert not re.search(r"subscri|gmp|grey", f["label"], re.I), "live figures do not belong in gold sets"


def test_underscored_metrics_and_other_contexts_are_handled(env, tmp_path):
    """Regressions from live runs 4 and 5: underscores hid matches; DRHP / lower-band / peer / dilution claims and
    segment / export revenue were wrongly counted as contradicting gold facts."""
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    gold = {"company": "t", "verdict": {"listing": "APPLY", "long_term": ["AVOID"]}, "facts": [
        {"id": "px", "label": "Upper band", "patterns": [r"price band|upper band"], "period": None, "value": 272,
         "unit": "INR", "tolerance": 0.005, "importance": "high"},
        {"id": "size", "label": "Issue size", "patterns": ["offer size|issue size"], "period": None,
         "value": 5520, "unit": "INR million", "tolerance": 0.005, "importance": "high"},
        {"id": "ronw", "label": "RoNW FY26", "patterns": [r"ronw"], "period": r"fy ?2026", "value": 25.84,
         "unit": "%", "tolerance": 0.005, "importance": "normal"},
        {"id": "cfo", "label": "CFO FY26", "patterns": [r"\bcfo\b"], "period": r"fy ?2026", "value": -266.44,
         "unit": "INR million", "tolerance": 0.005, "importance": "high"},
        {"id": "rev", "label": "Revenue FY26", "patterns": [r"revenue from operations|\brevenue\b"],
         "period": r"fy ?2026", "value": 11716.54, "unit": "INR million", "tolerance": 0.005, "importance": "high"}]}  # fmt: skip
    with session_scope() as s:
        co = get_or_create_company(s, "ev2-" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:8], "E")
        r = ResearchRun(company_id=co.id, kind="ipo_report", manifest={})
        s.add(r)
        s.flush()
        rows = [("upper_price_band", "272", "INR", "RHP", "Upper price band ₹272"),
                ("drhp_offer_size", "7000", "INR million", "DRHP Jul 2025", "DRHP offer size ₹7,000m"),
                ("rhp_offer_size", "5520", "INR million", "RHP Sep 2026", "RHP offer size ₹5,520m"),
                ("peer_ronw", "24.58", "%", "FY2026", "Polycab India FY2026 RoNW"),
                ("ronw", "25.84", "%", "FY2026", "RoNW FY2026"),
                ("cfo", "421.81", "INR million", "FY2024", "CFO ₹421.81m in FY2024, in contrast to FY2026"),
                ("cfo", "-266.44", "INR million", "FY2026", "CFO FY2026"),
                # live run 5: segment and export revenue were counted as contradicting total revenue
                ("revenue_networking_cables_segment", "9165.65", "INR million", "FY2026", "Networking revenue"),
                ("export_revenue", "1084.85", "INR million", "FY2026", "Export revenue FY2026"),
                ("revenue_from_operations", "11716.54", "INR million", "FY2026", "Revenue FY2026")]  # fmt: skip
        ids = []
        for m, v, u, per, st in rows:
            c = Claim(run_id=r.id, stream="x", claim_type="numeric", metric=m, value=Decimal(v), unit=u, period=per,
                      statement=st, status="verified")  # fmt: skip
            s.add(c)
            s.flush()
            ids.append(c.id)
        report = " ".join(f"[C{i}]" for i in ids)
        s.add(AgentStep(run_id=r.id, key="synthesis", stage="synthesis", role="synthesizer", status="done",
                        output={"report_markdown": report}))  # fmt: skip
        res = evaluate(s, r.id, gold, report)
    assert all(f.found for f in res.facts), [f.id for f in res.facts if not f.found]
    assert not res.contradicted, [(f.id, f.contradicted_in_report) for f in res.contradicted]


def test_period_exclusion_and_exclude_unless(env, tmp_path):
    """Live INFY run 8: an annual consolidated claim was dropped because its statement mentioned the quarter it was
    booked in, or the standalone comparative. Quarter exclusion now looks at the period field only, and a
    'standalone' exclusion is lifted when the claim says 'consolidated'."""
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    fact = {"id": "lc", "label": "Labour Codes FY26", "patterns": [r"labou?r code|exceptional"], "period": r"fy ?2026",
            "value": 1289, "unit": "INR crore", "tolerance": 0.005, "importance": "high",
            "exclude": r"\bpeer|standalone", "exclude_unless": "consolidated", "period_exclude": r"\bq[1-4]\b"}  # fmt: skip
    gold = {"company": "t", "verdict": None, "facts": [fact]}
    with session_scope() as s:
        co = get_or_create_company(s, "pe-" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:8], "E")
        r = ResearchRun(company_id=co.id, kind="stock_report", manifest={})
        s.add(r)
        s.flush()
        rows = [("labour_codes_exceptional_charge", "Q3 FY26", "1289", "Q3 FY26 charge", "verified"),
                ("exceptional_item_labour_codes", "FY2026", "1146", "standalone charge FY26", "verified"),
                ("exceptional_item_labour_codes", "FY2026", "1289",
                 "Consolidated Labour Codes charge in Q3, FY26 total (standalone 1,146)", "verified")]  # fmt: skip
        ids = []
        for m, per, v, st, status in rows:
            c = Claim(run_id=r.id, stream="x", claim_type="numeric", metric=m, value=Decimal(v), unit="INR crore",
                      period=per, statement=st, status=status)  # fmt: skip
            s.add(c)
            s.flush()
            ids.append(c.id)
        res = evaluate(s, r.id, gold, " ".join(f"[C{i}]" for i in ids))
    f = res.facts[0]
    assert (
        f.found and f.claim_ids == [ids[2]] and not f.contradicted_in_report
    )  # the standalone 1,146 is not a contradiction
    assert res.verdict_agrees is None


def test_foreign_currency_units_never_get_a_rupee_scale():
    """Live INFY run 8: USD revenue claims were compared with an INR gold fact and counted as contradictions."""
    from finresearch.evals.gold import _to_gold_unit
    from finresearch.verify.gate import rupee_scale

    assert rupee_scale("USD million") is None and rupee_scale("US$ mn") is None and rupee_scale("EUR") is None
    assert rupee_scale("INR crore") == Decimal(10_000_000) and rupee_scale("₹ million") == Decimal(1_000_000)
    assert _to_gold_unit(Decimal(20158), "USD million", "INR crore") is None


def test_derived_variants_do_not_contradict_the_reported_figure(env, tmp_path):
    """Live INFY run 9: adjusted / normalised EPS, profit before exceptional items, interim and final dividend parts
    and a standalone profit were counted as contradicting the reported consolidated figures."""
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    facts = [
        {"id": "eps", "label": "EPS", "patterns": [r"\beps\b|earnings per share"], "period": r"fy ?2026", "value": 71.58,
         "unit": "INR", "tolerance": 0.005, "importance": "high", "exclude": r"\bpeer|standalone|diluted",
         "exclude_unless": "consolidated"},
        {"id": "exc", "label": "Exceptional", "patterns": [r"labou?r code|exceptional"], "period": r"fy ?2026",
         "value": 1289, "unit": "INR crore", "tolerance": 0.005, "importance": "high", "exclude": r"\bpeer"},
        {"id": "pat", "label": "PAT", "patterns": [r"net profit|profit for the year"], "period": r"fy ?2026",
         "value": 29474, "unit": "INR crore", "tolerance": 0.005, "importance": "high", "exclude": r"standalone",
         "exclude_unless": "consolidated"},
        {"id": "pbt", "label": "PBT before exceptional", "patterns": [r"profit before exceptional"], "period": r"fy ?2026",
         "value": 41284, "unit": "INR crore", "tolerance": 0.005, "importance": "normal", "exclude": r"\bpeer"}]  # fmt: skip
    gold = {"company": "t", "verdict": None, "facts": facts}
    rows = [("adjusted_fy26_basic_eps", "72.06", "INR/share", "Adjusted EPS"),
            ("normalised_basic_eps", "71.13", "INR", "Normalised EPS"),
            ("basic_eps", "71.58", "INR", "Consolidated basic EPS"),
            ("profit_before_exceptional_items_and_tax", "41284", "INR crore", "Consolidated PBT before exceptional"),
            ("standalone_net_profit", "29211", "INR crore", "Standalone net profit, vs consolidated 29,474"),
            ("exceptional_items", "1289", "INR crore", "Consolidated exceptional items")]  # fmt: skip
    with session_scope() as s:
        co = get_or_create_company(s, "dv-" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:8], "E")
        r = ResearchRun(company_id=co.id, kind="stock_report", manifest={})
        s.add(r)
        s.flush()
        ids = []
        for m, v, u, st in rows:
            c = Claim(run_id=r.id, stream="x", claim_type="numeric", metric=m, value=Decimal(v), unit=u, period="FY2026",
                      statement=st, status="verified")  # fmt: skip
            s.add(c)
            s.flush()
            ids.append(c.id)
        res = evaluate(s, r.id, gold, " ".join(f"[C{i}]" for i in ids))
    by = {f.id: f for f in res.facts}
    assert not res.contradicted, [(f.id, f.contradicted_in_report) for f in res.contradicted]
    assert (
        by["eps"].found and by["exc"].found and by["pbt"].found
    )  # "before" is allowed when the fact names it
    assert (
        ids[4] not in by["pat"].claim_ids
    )  # a standalone metric is excluded even if the statement says consolidated


def test_require_keeps_peers_figures_out(env, tmp_path):
    """Live fund run 11: category peers' volatility and drawdown were counted as contradicting Axis Midcap's."""
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    fact = {"id": "dd", "label": "Max drawdown", "patterns": [r"drawdown"], "period": r"5y|2021-09", "value": -20.3446,
            "unit": "%", "tolerance": 0.005, "importance": "normal", "exclude": r"\bpeer", "require": r"axis midcap"}  # fmt: skip
    with session_scope() as s:
        co = get_or_create_company(s, "rq-" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:8], "E")
        r = ResearchRun(company_id=co.id, kind="fund_report", manifest={})
        s.add(r)
        s.flush()
        ids = []
        for st, v in (("ITI Mid Cap Fund Direct Growth max drawdown over 2021-09-20 to 2026-09-28", "-22.66"),
                      ("Axis Midcap Fund max drawdown over 5y", "-20.3446")):  # fmt: skip
            c = Claim(run_id=r.id, stream="x", claim_type="numeric", metric="max_drawdown", value=Decimal(v), unit="%",
                      period="2021-09-20 to 2026-09-28", statement=st, status="verified")  # fmt: skip
            s.add(c)
            s.flush()
            ids.append(c.id)
        res = evaluate(
            s, r.id, {"company": "t", "verdict": None, "facts": [fact]}, " ".join(f"[C{i}]" for i in ids)
        )
    assert res.facts[0].found and res.facts[0].claim_ids == [ids[1]] and not res.contradicted

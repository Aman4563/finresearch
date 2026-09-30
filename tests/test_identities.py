"""Accounting-identity and scale checks (verify/identities.py): seeded errors, golden tolerances, live-run replays."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from finresearch.verify.identities import (
    canonical,
    check_claims,
    parse_growth_period,
    parse_period,
    unit_kind,
)

EVAL = Path(__file__).parent / "fixtures" / "eval"
_ids = iter(range(1, 10_000))


def c(metric, value, unit="INR crore", period="FY2026", *, status="verified", importance="high", statement="",
      doc="RHP", cid=None):  # fmt: skip
    return {"id": cid or next(_ids), "metric": metric, "value": str(value), "unit": unit, "period": period,
            "status": status, "importance": importance, "statement": statement, "claim_type": "numeric",
            "stream": "financials", "citations": [{"document_title": doc}]}  # fmt: skip


def by_family(rep, family):
    return [x for x in rep.checks if x.family == family]


# --------------------------------------------------------------------------- parsing
@pytest.mark.parametrize(("raw", "want"), [
    ("FY2026", "FY2026"), ("FY26", "FY2026"), ("FY2024-25", "FY2025"), ("Q1 FY2027", "Q1FY2027"),
    ("Q1 FY27 (three months ended 2026-06-30)", "Q1FY2027"), ("FY2026 (as at 2026-03-31)", "FY2026"),
    ("2026-06-30", "Q1FY2027"), ("Mar-26", "FY2026"), ("FY2026, consolidated", "FY2026"),
    ("TTM Q2FY26-Q1FY27", None), ("FY2024-FY2026", None), ("FY2026 vs FY2025", None), ("2026-09-28", None),
    ("floor price ₹258, FY2026", None), ("FY2025 Q1 (30-Jun-2024)", None), ("FY2027E (1-year forward)", None),
])  # fmt: skip
def test_parse_period(raw, want):
    assert parse_period(raw) == want


def test_growth_periods_and_aliases():
    assert parse_growth_period("FY2026 vs FY2025", "revenue_growth_fy26") == ("FY2025", "FY2026", 1)
    assert parse_growth_period("FY2024-FY2026", "revenue_cagr_fy24_26") == ("FY2024", "FY2026", 2)
    assert parse_growth_period("FY2022-FY2026", "pat_cagr_inr_fy22_fy26") == ("FY2022", "FY2026", 4)
    assert (
        canonical("profit_before_tax") == "pbt" and canonical("net_cash_from_operating_activities") == "cfo"
    )
    assert canonical("revenue_networking_cables_segment") is None  # a segment is not total revenue
    assert unit_kind("INR million") == ("money", "INR", 10**6)
    assert unit_kind("USD million")[:2] == ("money", "USD")
    assert unit_kind("INR per share")[0] == "per_share" and unit_kind("%")[0] == "pct"
    assert unit_kind("INR million per month")[0] == "other"


# --------------------------------------------------------------------------- golden tolerances
def pnl(total="1181.67", **kw):
    return [c("revenue_from_operations", "1171.65", **kw), c("other_income", "10.02", **kw),
            c("total_income", total, **kw)]  # fmt: skip


def test_pnl_identity_passes_exactly_and_within_rounding():
    for total in ("1181.67", "1181.68", "1181.66"):  # printed at 2 dp: a 1-paisa-crore rounding gap is fine
        (chk,) = by_family(check_claims(pnl(total)), "pnl_total_income")
        assert chk.status == "pass", chk.detail


def test_pnl_identity_warns_on_a_small_gap_and_fails_on_a_large_one():
    (warn,) = by_family(check_claims(pnl("1190.00")), "pnl_total_income")  # 0.7 % apart
    assert warn.status == "warn" and not warn.hard
    (fail,) = by_family(check_claims(pnl("1300.00")), "pnl_total_income")  # 9 % apart, all verified
    assert fail.status == "fail" and fail.hard
    (soft,) = by_family(check_claims(pnl("1300.00", status="needs_review")), "pnl_total_income")
    assert soft.status == "fail" and not soft.hard  # unverified operands never make a hard (blocking) failure


def test_full_pnl_chain_with_exceptional_items_either_sign():
    """Infosys FY2026 (run 9): 178650 + 4322 = 182972; − 141688 = 41284; − 1289 exceptional = 39995; − 10521 = 29474."""
    rows = [c("revenue_from_operations", 178650), c("other_income", 4322), c("total_income", 182972),
            c("total_expenses", 141688), c("profit_before_exceptional_items_and_tax", 41284),
            c("exceptional_items", 1289), c("profit_before_tax", 39995), c("tax_expense", 10521),
            c("profit_for_the_year", 29474)]  # fmt: skip
    rep = check_claims(rows)
    assert {x.family for x in rep.checks} >= {"pnl_total_income", "pnl_pbt", "pnl_exceptional", "pnl_pat"}
    assert all(x.status == "pass" for x in rep.checks), [x.detail for x in rep.failing()]
    rows[5] = c("exceptional_items", -1289)  # recorded as a signed charge instead
    assert all(x.status == "pass" for x in check_claims(rows).checks)


def test_pbt_gap_without_exceptional_items_is_only_a_warning():
    rows = [c("total_income", 1000), c("total_expenses", 800), c("pbt", 150)]
    (chk,) = by_family(check_claims(rows), "pnl_pbt")
    assert chk.status == "warn" and "exceptional" in (chk.hint or "")


def test_eps_identity_golden():
    rows = [c("basic_eps", "71.58", "INR per share"), c("weighted_avg_basic_shares", "4112814745", "shares"),
            c("profit_attributable_to_owners", 29440)]  # fmt: skip
    (chk,) = by_family(check_claims(rows), "eps_identity")
    assert chk.status == "pass" and chk.diff_pct < 0.01
    rows[0] = c("basic_eps", "75.00", "INR per share")  # 4.6 % off
    (bad,) = by_family(check_claims(rows), "eps_identity")
    assert bad.status == "fail"


def test_cash_roll_forward_uses_previous_year_close():
    rows = [c("cash_and_equivalents", "14.52", "INR million", "FY2025"), c("cfo", "-266.44", "INR million"),
            c("cfi", "-635.75", "INR million"), c("cff", "1016.61", "INR million"),
            c("cash_and_equivalents", "128.94", "INR million")]  # fmt: skip
    (chk,) = by_family(check_claims(rows), "cash_rollforward")
    assert chk.status == "pass"
    rows[-1] = c("cash_and_equivalents", "228.94", "INR million")
    (bad,) = by_family(check_claims(rows), "cash_rollforward")
    assert bad.status == "warn" and "overdraft" in bad.hint  # never hard: overdrafts / FX may explain it


def test_balance_sheet_balances():
    ok = [c("total_assets", 155967), c("total_equity", 93297), c("current_liabilities", 52322),
          c("non_current_liabilities", 10348)]  # fmt: skip
    assert [x.status for x in by_family(check_claims(ok), "bs_balance")] == ["pass"]
    ok[-1] = c("non_current_liabilities", 20348)
    assert [x.status for x in by_family(check_claims(ok), "bs_balance")] == ["fail"]


def test_margins_and_growth_recomputed_with_accepted_definitions():
    rows = [
        c("revenue_from_operations", "11716.54", "INR million"),
        c("total_income", "11816.72", "INR million"),
        c("ebitda", "963.97", "INR million"),
        c("ebitda_margin", "8.23", "%"),
        c("pat", "535.61", "INR million"),
        c("pat_margin", "4.53", "%"),  # on total income
        c("pat", "533.21", "INR million", "FY2025"),
        c("pat_growth_fy26", "0.92", "%", "FY2026 vs FY2025"),
    ]  # the live run-5 mismatch (0.45 %)
    rep = check_claims(rows)
    margins = by_family(rep, "margin")
    assert {m.status for m in margins} == {"pass"} and any("total_income" in m.formula for m in margins)
    (g,) = by_family(rep, "growth")
    assert (
        g.status == "fail" and not g.hard and abs(g.expected - 0.0045) < 0.0001
    )  # 0.47 pp: flagged, not blocking


# --------------------------------------------------------------------------- seeded errors
def test_seeded_lakh_crore_swap_is_diagnosed():
    rows = [c("revenue_from_operations", "1171.65"), c("other_income", "10.02", "INR lakh"),  # should be crore
            c("total_income", "1181.67")]  # fmt: skip
    (chk,) = by_family(check_claims(rows), "pnl_total_income")
    assert chk.status == "fail" and "10^2" in chk.hint and "unit slip" in chk.hint


def test_seeded_scale_shift_between_two_claims():
    a = c("pat", "535.61", "INR million", cid=9001)
    b = c("pat", "535.61", "INR crore", cid=9002, doc="Annual report")
    (chk,) = by_family(check_claims([a, b]), "scale_shift")
    assert chk.status == "fail" and chk.hard and set(chk.claim_ids) == {9001, 9002}
    # the same amount in different units, and ₹ vs US$ at a real exchange rate, are NOT scale shifts
    same = [c("anchor_amount", "1656.1", "INR million"), c("anchor_amount", "1656100000", "INR")]
    fx = [c("revenue", 178650, "INR crore"), c("revenue", 20158, "USD million")]
    assert not by_family(check_claims(same), "scale_shift") and not by_family(check_claims(fx), "scale_shift")


def test_seeded_currency_label_mix_up():
    rows = [c("revenue", 20158, "INR million"), c("revenue", 20158, "USD million")]
    (chk,) = by_family(check_claims(rows), "scale_shift")
    assert "US$" in chk.title and chk.hard


def test_seeded_standalone_vs_consolidated():
    rows = [c("pbt", 200, period="FY2026, consolidated"), c("tax_expense", 50, period="FY2026, consolidated"),
            c("pat", 120, period="FY2026, consolidated"),  # wrong: the standalone PAT, labelled consolidated
            c("pbt", 160, period="FY2026 (standalone)"), c("tax_expense", 40, period="FY2026 (standalone)")]  # fmt: skip
    rep = check_claims(rows)
    cons = next(x for x in by_family(rep, "pnl_pat") if x.basis == "consolidated")
    assert cons.status == "fail"
    # the unlabelled variant: equals the standalone figure while a different consolidated one exists
    rows2 = [c("pat", 150, period="FY2026, consolidated"), c("pat", 120, period="FY2026 (standalone)"),
             c("pat", 120, period="FY2026", cid=9101)]  # fmt: skip
    (mix,) = by_family(check_claims(rows2), "basis_mix")
    assert mix.claim_ids[0] == 9101 and mix.status == "warn"


def test_seeded_standalone_operand_is_named_in_the_hint():
    rows = [c("pbt", 200, period="FY2026, consolidated"), c("tax_expense", 80, period="FY2026, consolidated"),
            c("pat", 150, period="FY2026, consolidated"), c("tax_expense", 50, period="FY2026 (standalone)")]  # fmt: skip
    chk = next(x for x in by_family(check_claims(rows), "pnl_pat") if x.basis == "consolidated")
    assert chk.status == "fail" and "standalone vs consolidated" in chk.hint


def test_seeded_unrestated_eps_after_a_bonus_issue():
    """Orient Cables had a 9:1 bonus (6-Jan-2025): every period's EPS must be on the post-bonus share count."""
    rows = [c("bonus_issue", "91831500", "shares", "2025-01-06"),
            c("pat", "400.69", "INR million", "FY2024"), c("basic_eps", "39.30", "INR per share", "FY2024"),  # seeded
            c("pat", "533.21", "INR million", "FY2025"), c("basic_eps", "5.23", "INR per share", "FY2025"),
            c("pat", "535.61", "INR million", "FY2026"), c("basic_eps", "5.27", "INR per share", "FY2026")]  # fmt: skip
    rep = check_claims(rows)
    bad = [x for x in by_family(rep, "eps_basis") if x.period == "FY2024"]
    assert (
        bad and bad[0].status == "fail" and bad[0].hard and "10" in bad[0].hint and "bonus" in bad[0].detail
    )
    assert [x.status for x in by_family(rep, "eps_basis") if x.period == "FY2025"] == ["pass"]
    rows[2] = c("basic_eps", "3.93", "INR per share", "FY2024")  # the restated figure the RHP prints
    assert {x.status for x in by_family(check_claims(rows), "eps_basis")} == {"pass"}


def test_cross_document_difference_is_flagged():
    rows = [c("revenue_from_operations", "8249.58", "INR million", "FY2025", doc="RHP (restated)"),
            c("revenue_from_operations", "8120.00", "INR million", "FY2025", doc="Annual report FY2025")]  # fmt: skip
    (chk,) = by_family(check_claims(rows), "cross_document")
    assert chk.status == "warn" and "restatement" in chk.hint


def test_sanity_bands():
    rows = [c("pat_margin", "457", "%"), c("pe_ratio", "-4", "x")]
    assert len(by_family(check_claims(rows), "sanity")) == 2


def test_contradicted_and_superseded_claims_are_ignored():
    rows = pnl("1300.00")
    rows[2]["status"] = "contradicted"
    assert not by_family(check_claims(rows), "pnl_total_income")


# --------------------------------------------------------------------------- live runs (replay fixtures)
def _fixture(name):
    return json.loads((EVAL / name).read_text())


def test_live_runs_identity_results():
    """Runs 5/9/11/12 as recorded: run 9's P&L chain and EPS identity pass; run 5's only failure is the stated PAT
    growth (0.92 % vs 0.45 % from its own PAT claims); funds and bonds have no applicable accounting identity."""
    r5 = check_claims(_fixture("orient-cables-run5.json")["claims"])
    assert r5.counts()["fail"] == 1 and by_family(r5, "growth")[0].status in ("pass", "fail")
    (bad,) = r5.failing()
    assert bad.family == "growth" and "pat" in bad.formula and not bad.hard
    assert {x.status for x in by_family(r5, "eps_basis")} == {"pass"}  # EPS restated for the 9:1 bonus
    r9 = check_claims(_fixture("infosys-run9.json")["claims"])
    assert not r9.failing() and len(r9.checks) >= 10
    assert {x.family for x in r9.checks} >= {"pnl_total_income", "pnl_pbt", "pnl_pat", "eps_identity"}
    for f in ("mf-120505-run11.json", "bond-ine027e07998-run12.json"):
        rep = check_claims(_fixture(f)["claims"])
        assert rep.checks == [] and rep.not_enough_inputs  # honest "not enough inputs", no false warnings


@pytest.mark.parametrize("name", ["orient-cables-run5.json", "infosys-run9.json", "mf-120505-run11.json",
                                  "bond-ine027e07998-run12.json"])  # fmt: skip
def test_replayed_live_runs_are_not_blocked_by_the_identity_policy(env, tmp_path, name):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep
    from finresearch.evals.replay import import_run
    from finresearch.verify.gate import check_report, identity_findings

    data = _fixture(name)
    with session_scope() as s:
        run_id = import_run(s, data, slug_suffix="-" + tmp_path.name[-8:])
        report = (s.query(AgentStep).filter_by(run_id=run_id, stage="synthesis", status="done")
                  .order_by(AgentStep.finished_at.desc()).first().output["report_markdown"])  # fmt: skip
        cited = {int(x) for x in re.findall(r"\[C(\d+)\]", report)}
        blocking, _ = identity_findings(s, run_id, cited)
        assert blocking == []
        assert check_report(s, run_id, report).ok


def _db_run(s, tmp_path, rows):
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    co = get_or_create_company(s, "ident-" + tmp_path.name[-8:], "Ident Ltd")
    run = ResearchRun(company_id=co.id, kind="ipo_report", status="running", manifest={})
    s.add(run)
    s.flush()
    ids = []
    for r in rows:
        cl = Claim(run_id=run.id, stream="financials", statement=r["statement"] or r["metric"], claim_type="numeric",
                   metric=r["metric"], value=r["value"], unit=r["unit"], period=r["period"],
                   importance=r["importance"], status=r["status"])  # fmt: skip
        s.add(cl)
        s.flush()
        ids.append(cl.id)
    return run.id, ids


def test_gate_blocks_a_cited_high_importance_scale_shift_and_warns_otherwise(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.verify.gate import check_report
    from finresearch.verify.identities import run_identities

    rows = [c("pat", "535.61", "INR million"), c("pat", "535.61", "INR crore", importance="normal")]
    with session_scope() as s:
        run_id, (a, b) = _db_run(s, tmp_path, rows)
        g = check_report(s, run_id, f"PAT was ₹535.61 million [C{a}].")
        assert not g.ok and "lakh / crore" in g.blocking[0]
        g2 = check_report(
            s, run_id, f"PAT was ₹535.61 crore [C{b}]."
        )  # only the normal-importance claim cited
        assert g2.ok and any("accounting check" in w for w in g2.warnings)
        g3 = check_report(s, run_id, "No figures here.")
        assert g3.ok and not any("accounting check" in w for w in g3.warnings)
        rep = run_identities(s, run_id, record=True)
        from finresearch.db.models import Claim

        assert rep.counts()["fail"] == 1 and s.get(Claim, a).checks["identities"][0].startswith(
            "fail: scale_shift"
        )


def test_gate_blocks_an_unrestated_eps_only_when_that_eps_is_cited(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.verify.gate import check_report

    rows = [c("pat", "400.69", "INR million", "FY2024"), c("basic_eps", "39.30", "INR per share", "FY2024"),
            c("pat", "533.21", "INR million", "FY2025"), c("basic_eps", "5.23", "INR per share", "FY2025"),
            c("pat", "535.61", "INR million", "FY2026"), c("basic_eps", "5.27", "INR per share", "FY2026")]  # fmt: skip
    with session_scope() as s:
        run_id, ids = _db_run(s, tmp_path, rows)
        ok = check_report(
            s, run_id, f"FY26 EPS ₹5.27 [C{ids[5]}] on PAT ₹535.61m [C{ids[4]}]; FY24 PAT [C{ids[0]}]."
        )
        assert ok.ok
        bad = check_report(s, run_id, f"FY24 EPS was ₹39.30 [C{ids[1]}].")
        assert not bad.ok and "not restated" in bad.blocking[0]

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
        # the only blocks left are evidence grades (#242: unchecked web quotes), never the identity policy
        assert not [b for b in check_report(s, run_id, report).blocking if "evidence is grade" not in b]


def _db_run(s, tmp_path, rows):
    from finresearch.db.models import Citation, Claim, ResearchRun
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
        # a document citation whose quote was found: evidence grade A, so only the identity policy decides
        cl.citations = [Citation(line_start=1, line_end=1, quote=r["metric"], quote_found=True)]
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


# --------------------------------------------------------------------------- statement checks (#518), all warn-level
NEW_FAMILIES = (
    "equity_rollforward",
    "da_tie",
    "tax_band",
    "margin_order",
    "capex_fixed_assets",
    "growth_outlier",
)
CONS, STAND = "FY2026, consolidated", "FY2026 (standalone)"


def not_checked(rep, text):
    return [m for m in rep.to_dict()["not_enough_inputs"] if text in m]


def one(rows, family):
    (chk,) = by_family(check_claims(rows), family)
    return chk


def equity(closing="1210", dividends="90", **kw):
    """Other equity 1000 at FY2025; + profit 300 − dividends 90 = 1210 at FY2026."""
    return [c("other_equity", 1000, period="FY2025", **kw), c("profit_attributable_to_owners", 300, **kw),
            c("dividends_paid", dividends, **kw), c("other_equity", closing, **kw)]  # fmt: skip


def test_equity_roll_forward_passes_with_stated_movements_and_either_dividend_sign():
    assert one(equity(), "equity_rollforward").status == "pass"
    assert one(equity(dividends="-90"), "equity_rollforward").status == "pass"  # recorded as a cash outflow
    # 1000 + 300 − 90 − 50 buyback + 10 share-based payments − 20 OCI = 1150
    rows = [*equity("1150"), c("buyback_of_equity_shares", 50), c("esop_charge", 10),
            c("other_comprehensive_income", -20)]  # fmt: skip
    chk = one(rows, "equity_rollforward")
    assert chk.status == "pass" and len(chk.claim_ids) == 7 and "buyback_out" in chk.formula
    # retained earnings roll on their own: 500 + 200 − 50 = 650
    re_rows = [c("retained_earnings", 500, period="FY2025"), c("pat", 200), c("dividends_paid", 50),
               c("retained_earnings", 650)]  # fmt: skip
    assert one(re_rows, "equity_rollforward").id.endswith(":retained_earnings")


def test_equity_roll_forward_gap_is_a_warning_never_a_block():
    bad = one(equity("1260"), "equity_rollforward")  # 1210 expected: 4.0 % apart, every operand verified
    assert bad.status == "warn" and not bad.hard and "other comprehensive income" in bad.hint
    # a lakh/crore slip is still named, but this check never fails: 90 lakh is 0.9 crore, the identity wants 90
    rows = equity()
    rows[2] = c("dividends_paid", 90, "INR lakh")
    slip = one(rows, "equity_rollforward")
    assert slip.status == "warn" and not slip.hard and "unit slip" in slip.hint


def test_equity_roll_forward_says_which_input_is_missing():
    no_div = check_claims([r for r in equity() if r["metric"] != "dividends_paid"])
    assert not by_family(no_div, "equity_rollforward")  # an absent dividend is never read as "no dividend"
    assert not_checked(no_div, "equity roll-forward — not checked (FY2026): dividends paid")
    no_open = check_claims(equity()[1:])
    assert not by_family(no_open, "equity_rollforward") and not_checked(no_open, "the opening balance")
    assert not_checked(check_claims(pnl()), "equity roll-forward — not checked: no other equity")
    # opening balance standalone, closing consolidated: never rolled into each other
    mixed = equity()
    mixed[0] = c("other_equity", 1000, period="FY2025 (standalone)")
    mixed[3] = c("other_equity", 1210, period=CONS)
    rep = check_claims(mixed)
    assert not by_family(rep, "equity_rollforward") and not_checked(rep, "FY2026 consolidated")


def test_da_in_the_cash_flow_ties_to_the_pnl_charge():
    rows = [c("depreciation", "147.20", "INR million"),
            c("depreciation_cash_flow", "147.20", "INR million")]  # fmt: skip
    assert one(rows, "da_tie").status == "pass"
    # the claim's own words place a plain "depreciation" in the cash-flow statement; an outflow sign does not matter
    rows[1] = c(
        "depreciation",
        "-147.20",
        "INR million",
        statement="Depreciation added back in the cash-flow statement",
    )
    assert one(rows, "da_tie").status == "pass"
    rows[1] = c("depreciation_cash_flow", "157.20", "INR million")  # planted: 10 more than the charge (6.4 %)
    bad = one(rows, "da_tie")
    assert bad.status == "warn" and not bad.hard and bad.diff_pct > 6


def test_da_tie_missing_side_and_mixed_bases_are_not_checked():
    only_pnl = check_claims([c("depreciation", "147.20", "INR million")])
    assert not by_family(only_pnl, "da_tie")
    assert not_checked(
        only_pnl, "D&A tie (cash flow vs P&L) — not checked (FY2026): the add-back in the cash-flow"
    )
    mixed = check_claims(
        [c("depreciation", 100, period=STAND), c("depreciation_cash_flow", 180, period=CONS)]
    )
    assert not by_family(mixed, "da_tie") and len(not_checked(mixed, "D&A tie")) == 2
    assert not_checked(check_claims(pnl()), "D&A tie (cash flow vs P&L) — not checked: no depreciation")


def test_statutory_tax_rates_and_band_are_the_hand_computed_ones():
    """rate × (1 + surcharge) × 1.04 cess, by hand: 115BAB 0.15 × 1.10 × 1.04; MAT (115JB) 0.15 × 1.12 × 1.04;
    115BAA 0.22 × 1.10 × 1.04; the 25 % and 30 % slabs with the 12 % surcharge. Band: lowest − 3 pp … highest + 3 pp."""
    from decimal import Decimal as D

    from finresearch.verify.identities import STATUTORY_TAX_RATES, TAX_BAND

    assert [r for _, r in STATUTORY_TAX_RATES] == [
        D("0.1716"),
        D("0.17472"),
        D("0.25168"),
        D("0.2912"),
        D("0.34944"),
    ]
    assert all(sec in " ".join(n for n, _ in STATUTORY_TAX_RATES) for sec in ("115BAA", "115BAB", "115JB"))
    assert (D("0.1416"), D("0.37944")) == TAX_BAND


def tax(amount, pbt=1000, **kw):
    return [c("profit_before_tax", pbt, **kw), c("tax_expense", amount, **kw)]


def test_effective_tax_rate_band():
    ok = one(tax("251.68"), "tax_band")  # the 115BAA rate itself
    assert ok.status == "pass" and ok.formula == "tax ÷ pbt" and abs(ok.actual - 0.25168) < 1e-9
    assert one(tax("141.6"), "tax_band").status == "pass"  # 14.16 %: the edge is inside
    for amount in ("141", "380"):  # 14.10 % and 38.00 %: outside 14.16 %…37.944 %
        bad = one(tax(amount), "tax_band")
        assert bad.status == "warn" and not bad.hard and "s.115JAA" in bad.hint and "115BAA" in bad.tolerance


def test_tax_rate_outside_the_band_needs_an_explanation_claim():
    rows = [*tax(450), c("deferred_tax", 180, cid=7001)]  # 45 %, with a deferred-tax charge in the ledger
    chk = one(rows, "tax_band")
    assert chk.status == "pass" and "C7001 deferred_tax" in chk.detail and 7001 in chk.claim_ids
    # a nil exceptional item explains nothing; a reason for another basis does not count either
    assert one([*tax(450), c("exceptional_items", 0)], "tax_band").status == "warn"
    cons = [c("profit_before_tax", 1000, period=CONS), c("tax_expense", 450, period=CONS),
            c("deferred_tax", 180, period=STAND)]  # fmt: skip
    assert one(cons, "tax_band").status == "warn"


def test_tax_rate_implied_stated_and_not_checked():
    # no tax line: (726.00 − 535.61) ÷ 726.00 = 26.22 %, labelled as implied (live run 5, FY2026)
    implied = one([c("pbt", "726.00", "INR million"), c("pat", "535.61", "INR million")], "tax_band")
    assert implied.status == "pass" and "implied" in implied.formula and abs(implied.actual - 0.262245) < 1e-6
    stated = one([c("effective_tax_rate", 45, "%")], "tax_band")
    assert stated.status == "warn" and "as stated" in stated.formula
    loss = check_claims(tax(5, pbt=-100))
    assert not by_family(loss, "tax_band") and not_checked(loss, "PBT is zero or negative")
    alone = check_claims([c("profit_before_tax", 1000)])
    assert not by_family(alone, "tax_band")
    assert not_checked(alone, "effective tax rate — not checked (FY2026): tax expense")
    # consolidated PBT with a standalone tax line: not divided into each other
    mixed = check_claims([c("profit_before_tax", 1000, period=CONS), c("tax_expense", 450, period=STAND)])
    assert not by_family(mixed, "tax_band") and not_checked(mixed, "FY2026 consolidated")


def margins(gross=40, ebitda=20, pat=10, **kw):
    return [
        c("gross_margin", gross, "%", **kw),
        c("ebitda_margin", ebitda, "%", **kw),
        c("pat_margin", pat, "%", **kw),
    ]


def test_margin_order_stated_or_computed():
    assert one(margins(), "margin_order").status == "pass"
    assert one(margins(ebitda=20, pat=20), "margin_order").status == "pass"  # equal is in order
    parts = [c("revenue_from_operations", 1000), c("gross_profit", 400), c("ebitda", 200), c("pat", 100)]
    chk = one(parts, "margin_order")  # 40 % ≥ 20 % ≥ 10 % on revenue
    assert chk.status == "pass" and "gross 40.00% ≥ EBITDA 20.00% ≥ PAT 10.00%" in chk.detail
    bad = one(margins(pat=25), "margin_order")
    assert (
        bad.status == "warn"
        and not bad.hard
        and "PAT margin 25.00% is above EBITDA margin 20.00%" in bad.detail
    )
    assert "gross margin 15.00% is above" not in one(margins(gross=15), "margin_order").detail
    assert (
        "EBITDA margin 20.00% is above gross margin 15.00%" in one(margins(gross=15), "margin_order").detail
    )
    two = one(margins()[1:], "margin_order")  # two of the three are enough; the third is named
    assert two.status == "pass" and "gross margin not in the ledger: not checked" in two.detail


def test_margin_order_skips_lenders_and_never_mixes_bases():
    bank = check_claims([*margins(pat=25), c("gross_npa_pct", "2.5", "%", cid=7100)])
    assert not by_family(bank, "margin_order")
    (line,) = not_checked(bank, "skipped")
    assert "C7100 gross_npa_pct" in line and "bank, NBFC or insurer" in line
    # a peer's lender metric does not make this company a lender
    assert one([*margins(pat=25), c("peer_nim_hdfc_bank", "3.4", "%")], "margin_order").status == "warn"
    mixed = check_claims([c("ebitda_margin", 20, "%", STAND), c("pat_margin", 25, "%", CONS)])
    assert not by_family(mixed, "margin_order") and len(not_checked(mixed, "a second margin")) == 1
    assert not_checked(check_claims([c("ebitda_margin", 20, "%")]), "not checked (FY2026): a second margin")


def block(capex, cwip=True, **kw):
    """Gross block 1000 → 1400 and CWIP 100 → 250 over FY2026: a rise of 550 with CWIP, 400 without."""
    rows = [
        c("gross_block", 1000, period="FY2025", **kw),
        c("gross_block", 1400, **kw),
        c("capex", capex, **kw),
    ]
    return [*rows, c("cwip", 100, period="FY2025", **kw), c("cwip", 250, **kw)] if cwip else rows


def test_capex_against_the_rise_in_gross_block_and_cwip():
    ok = one(block(600), "capex_fixed_assets")  # 600 vs 550: 50 ÷ 600 = 8.3 %
    assert (
        ok.status == "pass"
        and ok.expected == 550e7  # ₹550 crore, in rupees
        and abs(ok.diff_pct - 8.333) < 0.001
        and len(ok.claim_ids) == 5
    )
    assert one(block(-600), "capex_fixed_assets").status == "pass"  # the cash-flow line as an outflow
    bad = one(block(900), "capex_fixed_assets")  # 900 vs 550: 350 ÷ 900 = 38.9 %, above the 25 % band
    assert bad.status == "warn" and not bad.hard and "disposals" in bad.hint
    # without CWIP at both year ends only the block moves: 500 vs 400 is 20 % (inside), 550 vs 400 is 27.3 %
    assert one(block(500, cwip=False), "capex_fixed_assets").status == "pass"
    edge = one(block(550, cwip=False), "capex_fixed_assets")
    assert edge.status == "warn" and "CWIP is not held" in edge.detail and len(edge.claim_ids) == 3


def test_capex_missing_inputs_and_mixed_bases_are_not_checked():
    no_open = check_claims(block(600, cwip=False)[1:])
    assert not by_family(no_open, "capex_fixed_assets")
    assert not_checked(
        no_open, "capex vs gross block — not checked (FY2026): gross block at the previous year end"
    )
    no_capex = check_claims(block(600, cwip=False)[:2])
    assert not by_family(no_capex, "capex_fixed_assets") and not_checked(
        no_capex, "not checked (FY2026): capex"
    )
    mixed = check_claims([c("gross_block", 1000, period="FY2025 (standalone)"), c("gross_block", 1400, period=CONS),
                          c("capex", 600, period=CONS)])  # fmt: skip
    assert not by_family(mixed, "capex_fixed_assets") and not_checked(mixed, "FY2026 consolidated")
    # unlabelled capex and block, CWIP standalone at one year end and consolidated at the other: never differenced
    cwip = [
        *block(500, cwip=False),
        c("cwip", 100, period="FY2025 (standalone)"),
        c("cwip", 900, period=CONS),
    ]
    chk = one(cwip, "capex_fixed_assets")
    assert chk.status == "pass" and "CWIP is not held" in chk.detail and len(chk.claim_ids) == 3


def test_growth_above_100_pct_or_a_sign_change_needs_an_explanation():
    def rev(now, then, **kw):
        return [
            c("revenue_from_operations", then, period="FY2025", **kw),
            c("revenue_from_operations", now, **kw),
        ]

    assert one(rev(1500, 1000), "growth_outlier").status == "pass"  # +50 %
    assert one(rev(2000, 1000), "growth_outlier").status == "pass"  # exactly +100 % is not "above"
    up = one(rev(2001, 1000), "growth_outlier")
    assert (
        up.status == "warn"
        and not up.hard
        and "+100.1%" in up.detail
        and "explanation in the text" in up.hint
    )
    turn = one([c("pat", -50, period="FY2025"), c("pat", 30)], "growth_outlier")
    assert turn.status == "warn" and "loss to profit" in turn.detail
    assert "profit to loss" in one([c("pat", 30, period="FY2025"), c("pat", -50)], "growth_outlier").detail
    assert (
        one([c("pat", -10, period="FY2025"), c("pat", -30)], "growth_outlier").status == "warn"
    )  # the loss tripled
    q = one([c("ebitda", 100, period="Q1 FY2026"), c("ebitda", 300, period="Q1 FY2027")], "growth_outlier")
    assert (
        q.status == "warn" and q.period == "Q1FY2027" and "+200.0%" in q.detail
    )  # same quarter a year earlier


def test_growth_outliers_never_compare_across_basis_currency_or_unit_kind():
    pairs = [
        [c("revenue_from_operations", 100, period="FY2025 (standalone)"), c("revenue_from_operations", 1000, period=CONS)],
        [c("revenue", 19277, "USD million", "FY2025"), c("revenue", 178650, "INR crore")],  # live run 9's two units
        [c("basic_eps", "64.50", "INR", "FY2025"), c("basic_eps", "171.58", "INR per share")],  # money vs per share
    ]  # fmt: skip
    for rows in pairs:
        rep = check_claims(rows)
        assert not by_family(rep, "growth_outlier") and not_checked(rep, "growth outliers — not checked")
    # a stated growth counts only when the two figures are not both held
    stated = [c("revenue_growth_fy26", 150, "%", "FY2026 vs FY2025")]
    assert one(stated, "growth_outlier").status == "warn"
    both = [*stated, c("revenue_from_operations", 1000, period="FY2025"), c("revenue_from_operations", 2500)]
    chk = one(both, "growth_outlier")
    assert chk.status == "warn" and len(chk.claim_ids) == 2


def test_statement_checks_on_the_live_runs():
    """Coverage of the six checks on runs 5/9/11/12 (the PR's replay table): what each fixture lets them check.
    No new warning on any of them; no fixture holds other equity, a cash-flow D&A or a gross block."""
    want = {"orient-cables-run5.json": {"tax_band": 4, "margin_order": 3, "growth_outlier": 10},
            "infosys-run9.json": {"tax_band": 2, "margin_order": 2, "growth_outlier": 14},
            "mf-120505-run11.json": {}, "bond-ine027e07998-run12.json": {}}  # fmt: skip
    for name, families in want.items():
        rep = check_claims(_fixture(name)["claims"])
        new = [x for x in rep.checks if x.family in NEW_FAMILIES]
        assert {f: len(by_family(rep, f)) for f in NEW_FAMILIES if by_family(rep, f)} == families, name
        assert all(x.status == "pass" and not x.hard for x in new), [
            x.detail for x in new if x.status != "pass"
        ]
        for missing in ("equity roll-forward — not checked", "D&A tie (cash flow vs P&L) — not checked",
                        "capex vs gross block — not checked"):  # fmt: skip
            assert not_checked(rep, missing), (name, missing)
    r5, r9 = (check_claims(_fixture(n)["claims"]) for n in ("orient-cables-run5.json", "infosys-run9.json"))
    assert all("implied" in x.formula for x in by_family(r5, "tax_band"))  # the IPO ledger holds no tax line
    assert sorted(x.formula[:9] for x in by_family(r9, "tax_band")) == ["(pbt − pa", "tax ÷ pbt"]
    bond = check_claims(_fixture("bond-ine027e07998-run12.json")["claims"])
    assert "crar_pct" in not_checked(bond, "margin ordering (gross ≥ EBITDA ≥ PAT) — skipped")[0]  # an NBFC


def test_gate_only_warns_on_a_statement_check_however_wrong_and_cited(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.verify.gate import check_report

    # every operand verified and high-importance: a 45 % tax rate, PAT up 5.5×, margins out of order, equity 4 % off
    rows = [c("profit_before_tax", 1000), c("tax_expense", 450), c("pat", 100, period="FY2025"), c("pat", 550),
            c("ebitda_margin", 20, "%"), c("pat_margin", 25, "%"), *equity("1260")]  # fmt: skip
    with session_scope() as s:
        run_id, ids = _db_run(s, tmp_path, rows)
        g = check_report(s, run_id, " ".join(f"[C{i}]" for i in ids))
        assert g.ok and not g.blocking
        for title in ("Effective tax rate inside the statutory band", "Year-on-year change in a headline line (PAT)",
                      "Margins in order", "Other equity roll-forward"):  # fmt: skip
            assert any(f"accounting check '{title}" in w and " warns " in w for w in g.warnings), title

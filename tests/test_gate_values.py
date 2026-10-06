"""Issue #217: a correctly cited but wrong number must not pass the value-in-source check.

Synthetic RHP / annual-report table snippets (made-up company and figures) in pdftotext -layout form: right-aligned
period columns, lakh / crore / million headers, bracketed negatives, standalone and consolidated page titles.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

W_LABEL, W_COL = 46, 16


def row(label: str, *cells: str, w: int = W_COL) -> str:
    return label.ljust(W_LABEL) + "".join(c.rjust(w) for c in cells)


LAKH_PNL = "\n".join([
    "                RESTATED CONSOLIDATED STATEMENT OF PROFIT AND LOSS",
    "(₹ in lakh)".rjust(W_LABEL + 3 * W_COL),
    row("Particulars", "Fiscal 2026", "Fiscal 2025", "Fiscal 2024"),
    row("Revenue from operations", "12,345.60", "10,234.50", "8,765.40"),
    row("Other income", "234.10", "198.70", "150.20"),
    row("Total income", "12,579.70", "10,433.20", "8,915.60"),
    row("Profit / (Loss) before tax", "1,234.00", "(456.30)", "321.90"),
    row("Profit / (Loss) for the year", "987.20", "(512.40)", "240.10"),
    row("Revenue growth (%)", "20.62", "16.76", "-"),
    row("EBITDA margin (%)", "14.10", "(2.35)", "9.80"),
])  # fmt: skip

# an annual-report standalone statement in ₹ crore with a Note column and a page break before the next page's table
AR_STANDALONE = "\n".join([
    "Standalone Balance Sheet",
    "(In ₹ crore)".rjust(W_LABEL + 3 * 24),
    row("Particulars", "Note", "As at March 31, 2026", "As at March 31, 2025", w=24),
    row("Trade receivables", "2.8", "45,678", "39,012", w=24),
    row("Cash and cash equivalents", "2.9", "12,004", "14,786", w=24),
    "\f" + row("Particulars", "", "", "", w=24),  # next page: a continuation table without a unit or period header
    row("Other financial assets", "", "4,321", "3,987", w=24),
])  # fmt: skip

PROSE = ("Our revenue from operations declined by 12.5% to ₹ 1,050.00 crore in Fiscal 2026, and we reported a net loss "
         "of ₹ 84.20 crore for Fiscal 2026.")  # fmt: skip


@pytest.fixture
def make_ledger(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Document, DocumentPage, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    def make(text: str) -> dict:
        txt = tmp_path / f"doc-{hashlib.sha1(text.encode()).hexdigest()[:8]}.txt"
        txt.write_text(text)
        lines = text.split("\n")
        key = hashlib.sha1(f"{tmp_path}{text}".encode()).hexdigest()
        with session_scope() as s:
            co = get_or_create_company(s, "gv-" + key[:10], "Gate Values Co")
            d = Document(company_id=co.id, kind="RHP", title="t", sha256=hashlib.sha256(key.encode()).hexdigest(),
                         local_path=str(txt), text_path=str(txt), bytes=1, pages=1)  # fmt: skip
            s.add(d)
            s.flush()
            s.add(DocumentPage(document_id=d.id, page_no=1, line_start=1, line_end=len(lines), text="",
                               char_count=0, text_source="pdftotext"))  # fmt: skip
            run = ResearchRun(company_id=co.id, kind="test", manifest={})
            s.add(run)
            s.flush()
            return {"run": run.id, "doc": d.id, "lines": lines}

    return make


def line_of(led, text):
    return next(i for i, x in enumerate(led["lines"], 1) if text in x)


def add(led, *, metric, value, unit, period, cite, statement=None, importance="normal", status="unverified"):
    from finresearch.db import session_scope
    from finresearch.db.models import Citation, Claim

    ln = line_of(led, cite)
    with session_scope() as s:
        c = Claim(run_id=led["run"], stream="financials", statement=statement or f"{metric} {value} {unit} {period}",
                  claim_type="numeric", metric=metric, value=Decimal(str(value)), unit=unit, period=period,
                  importance=importance, status=status)  # fmt: skip
        s.add(c)
        s.flush()
        s.add(Citation(claim_id=c.id, document_id=led["doc"], line_start=ln, line_end=ln,
                       quote=led["lines"][ln - 1].strip(), quote_found=True))  # fmt: skip
        return c.id


def gate(led):
    from finresearch.db import session_scope
    from finresearch.verify.gate import run_gate

    with session_scope() as s:
        return run_gate(s, led["run"])


def claim(cid):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim

    with session_scope() as s:
        c = s.get(Claim, cid)
        return c.status, dict(c.checks or {}), c.verifier_note or ""


# ------------------------------------------------------------------ 1. sign flips
def test_a_profit_claimed_against_a_bracketed_loss_is_a_sign_mismatch(make_ledger):
    led = make_ledger(LAKH_PNL)
    flip = add(led, metric="pat", value="512.40", unit="INR lakh", period="FY2025", cite="for the year",
               statement="Profit after tax was ₹512.40 lakh in Fiscal 2025.", status="verified")  # fmt: skip
    loss_word = add(led, metric="pat", value="512.40", unit="INR lakh", period="FY2025", cite="for the year",
                    statement="The company reported a net loss of ₹512.40 lakh in Fiscal 2025.")  # fmt: skip
    signed = add(led, metric="pat", value="-512.40", unit="INR lakh", period="FY2025", cite="for the year")
    r = gate(led)
    st, chk, note = claim(flip)
    assert chk["value_in_source"] is False and chk["value_check"] == "sign_mismatch"
    assert st == "needs_review" and "sign" in note  # a verified claim is demoted: the source shows the opposite sign
    assert flip in r.value_mismatches
    for ok in (loss_word, signed):
        assert claim(ok)[1]["value_in_source"] is True and claim(ok)[1]["value_check"] == "pass", ok


def test_a_negative_growth_claim_against_a_positive_growth_row(make_ledger):
    led = make_ledger(LAKH_PNL)
    neg = add(led, metric="revenue_growth", value="-20.62", unit="%", period="FY2026", cite="Revenue growth",
              statement="Revenue declined 20.62% in Fiscal 2026.")  # fmt: skip
    pos = add(led, metric="revenue_growth_yoy", value="20.62", unit="%", period="FY2026", cite="Revenue growth")
    margin_flip = add(led, metric="ebitda_margin", value="2.35", unit="%", period="FY2025", cite="EBITDA margin")
    gate(led)
    assert claim(neg)[1]["value_check"] == "sign_mismatch" and claim(neg)[0] == "needs_review"
    assert claim(pos)[1]["value_check"] == "pass" and claim(pos)[0] == "unverified"
    # "(2.35)" in a plain margin row is a negative margin; a bare 2.35 % claim has no sign evidence either way
    assert claim(margin_flip)[1]["value_check"] == "pass"


def test_prose_level_after_a_decline_is_not_negative(make_ledger):
    led = make_ledger(PROSE)
    level = add(led, metric="revenue", value="1050", unit="INR crore", period="FY2026", cite="declined",
                statement="Revenue from operations fell to ₹1,050 crore in Fiscal 2026.")  # fmt: skip
    drop = add(led, metric="revenue_growth", value="-12.5", unit="%", period="FY2026", cite="declined")
    grew = add(led, metric="revenue_growth", value="12.5", unit="%", period="FY2026", cite="declined",
               statement="Revenue grew 12.5% in Fiscal 2026.")  # fmt: skip
    loss = add(led, metric="net_loss", value="84.20", unit="INR crore", period="FY2026", cite="declined")
    profit = add(led, metric="pat", value="84.20", unit="INR crore", period="FY2026", cite="declined",
                 statement="Net profit was ₹84.20 crore in Fiscal 2026.")  # fmt: skip
    gate(led)
    for ok in (level, drop, loss):
        assert claim(ok)[1]["value_check"] == "pass", (ok, claim(ok))
    assert claim(grew)[1]["value_check"] == "sign_mismatch"
    assert claim(profit)[1]["value_check"] == "sign_mismatch"


def test_a_negative_quote_must_be_negative_in_the_cited_lines(tmp_path):
    from types import SimpleNamespace

    from finresearch.mcp_server.claims import quote_in_lines

    p = tmp_path / "t.txt"
    p.write_text(row("Profit / (Loss) for the year", "987.20", "512.40", "240.10") + "\n")
    doc = SimpleNamespace(text_path=str(p))
    assert quote_in_lines(doc, 1, 1, "Profit for the year 987.20 and (512.40)")[0] is False
    assert quote_in_lines(doc, 1, 1, "Loss -512.40 vs 987.20")[0] is False
    assert quote_in_lines(doc, 1, 1, "Profit 987.20 and 512.40")[0] is True


# ------------------------------------------------------------------ 2. source units
def test_a_crore_claim_does_not_verify_against_the_same_digits_in_a_lakh_table(make_ledger):
    led = make_ledger(LAKH_PNL)
    wrong = add(led, metric="revenue", value="12345.60", unit="INR crore", period="FY2026", cite="Revenue from",
                importance="high", status="verified")  # fmt: skip
    crore = add(led, metric="revenue", value="123.456", unit="INR crore", period="FY2026", cite="Revenue from")
    million = add(led, metric="revenue", value="1234.56", unit="INR million", period="FY2026", cite="Revenue from")
    r = gate(led)
    st, chk, note = claim(wrong)
    assert chk["value_check"] == "unit_mismatch" and chk["value_in_source"] is False
    assert st == "needs_review" and "lakh" in note and wrong in r.value_mismatches
    for ok in (crore, million):
        _, c, _ = claim(ok)
        assert c["value_check"] == "pass" and c["value_in_source"] is True and c["source_unit"] == "INR lakh"
        assert not c.get("value_warnings")


def test_unit_unknown_is_a_warning_and_never_a_pass_for_high_importance(make_ledger):
    led = make_ledger(AR_STANDALONE)
    high = add(led, metric="other_financial_assets", value="4321", unit="INR crore", period="FY2026",
               cite="Other financial assets", importance="high")  # fmt: skip
    normal = add(led, metric="other_financial_assets", value="4321", unit="INR crore", period="FY2026",
                 cite="Other financial assets")  # fmt: skip
    gate(led)
    st, chk, note = claim(high)
    # the unit header sits on the previous page: unknown, so a high-importance claim is not a pass
    assert "unit unknown" in chk["value_warnings"] and chk["value_in_source"] is False
    assert st == "needs_review" and "unit unknown" in note
    st, chk, _ = claim(normal)
    assert "unit unknown" in chk["value_warnings"] and chk["value_in_source"] is True and st == "unverified"


# ------------------------------------------------------------------ 3. neighbouring periods
def test_the_neighbouring_year_column_does_not_verify_a_claim(make_ledger):
    led = make_ledger(LAKH_PNL)
    neighbour = add(led, metric="revenue", value="10234.50", unit="INR lakh", period="FY2026",
                    cite="Revenue from", importance="high", status="verified")  # fmt: skip
    right = add(led, metric="revenue", value="10234.50", unit="INR lakh", period="FY2025", cite="Revenue from")
    fy24 = add(led, metric="revenue", value="87.654", unit="INR crore", period="FY2023-24", cite="Revenue from")
    r = gate(led)
    st, chk, note = claim(neighbour)
    assert chk["value_check"] == "period_mismatch" and st == "needs_review" and "FY2025 column" in note
    assert neighbour in r.value_mismatches
    assert claim(right)[1]["value_check"] == "pass" and claim(right)[1]["source_period"] == "FY2025"
    assert claim(fy24)[1]["value_check"] == "pass"


def test_balance_sheet_dates_and_note_columns(make_ledger):
    led = make_ledger(AR_STANDALONE)
    right = add(led, metric="trade_receivables_standalone", value="45678", unit="INR crore", period="FY2026",
                cite="Trade receivables")  # fmt: skip
    prior = add(led, metric="trade_receivables_standalone", value="45678", unit="INR crore", period="FY2025",
                cite="Trade receivables")  # fmt: skip
    gate(led)
    assert claim(right)[1]["value_check"] == "pass"
    assert claim(prior)[1]["value_check"] == "period_mismatch"


def test_a_period_that_cannot_be_aligned_is_unverified(make_ledger):
    led = make_ledger(AR_STANDALONE)
    high = add(led, metric="other_financial_assets", value="3987", unit="INR crore", period="FY2025",
               cite="Other financial assets", importance="high")  # fmt: skip
    gate(led)
    st, chk, _ = claim(high)
    assert "period unverified" in chk["value_warnings"] and chk["value_in_source"] is False
    assert st == "needs_review"


def test_quarter_and_year_columns_on_the_orient_layout(make_ledger):
    from pathlib import Path

    led = make_ledger((Path(__file__).parent / "fixtures" / "orient_rhp_pnl_layout.txt").read_text())
    q1_as_fy = add(led, metric="revenue", value="4891.56", unit="INR million", period="FY2026",
                   cite="Revenue from Operations")  # fmt: skip
    q1 = add(led, metric="revenue", value="4891.56", unit="INR million", period="Q1 FY2027",
             cite="Revenue from Operations")  # fmt: skip
    fy = add(led, metric="revenue", value="1171.654", unit="INR crore", period="FY2026", cite="Revenue from Operations")
    eps = add(led, metric="basic_eps", value="5.27", unit="INR per share", period="FY2026", cite="Basic (in ₹)")
    gate(led)
    assert claim(q1_as_fy)[1]["value_check"] == "period_mismatch"
    for ok in (q1, fy, eps):
        assert claim(ok)[1]["value_check"] == "pass" and not claim(ok)[1].get("value_warnings"), claim(ok)


# ------------------------------------------------------------------ 4. basis
def test_a_consolidated_claim_on_a_standalone_page_is_flagged(make_ledger):
    led = make_ledger(AR_STANDALONE)
    cons = add(led, metric="trade_receivables", value="45678", unit="INR crore", period="FY2026, consolidated",
               cite="Trade receivables")  # fmt: skip
    gate(led)
    st, chk, note = claim(cons)
    assert chk["value_check"] == "basis_mismatch" and chk["value_in_source"] is False
    assert st == "needs_review" and "standalone" in note


# ------------------------------------------------------------------ corrections and the publish gate
def test_a_sign_flipped_or_neighbouring_correction_is_not_auto_verified(make_ledger):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.verify.gate import apply_correction

    led = make_ledger(LAKH_PNL)
    bad = add(led, metric="pat", value="512.00", unit="INR lakh", period="FY2025", cite="for the year",
              status="contradicted")  # fmt: skip
    bad2 = add(led, metric="revenue", value="12000", unit="INR lakh", period="FY2025", cite="Revenue from",
               status="contradicted")  # fmt: skip
    with session_scope() as s:
        flip = apply_correction(s, s.get(Claim, bad), "512.40", "the RHP shows 512.40")
        right = apply_correction(s, s.get(Claim, bad), "-512.40", "the RHP shows a loss of 512.40")
        col = apply_correction(s, s.get(Claim, bad2), "12,345.60", "revenue row")
        assert flip.status == "needs_review" and flip.checks["value_check"] == "sign_mismatch"
        assert right.status == "verified" and right.checks["value_in_source"] is True
        assert col.status == "needs_review" and col.checks["value_check"] == "period_mismatch"


def test_publish_gate_blocks_a_cited_high_importance_hard_mismatch_even_if_reverified(make_ledger):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.verify.gate import check_report

    led = make_ledger(LAKH_PNL)
    hi = add(led, metric="revenue", value="10234.50", unit="INR lakh", period="FY2026", cite="Revenue from",
             importance="high")  # fmt: skip
    lo = add(led, metric="other_income", value="198.70", unit="INR lakh", period="FY2026", cite="Other income")
    gate(led)
    with session_scope() as s:  # a later verifier pass (cross-stream conflicts) marks both verified again
        for i in (hi, lo):
            s.get(Claim, i).status = "verified"
    with session_scope() as s:
        g = check_report(s, led["run"], f"Revenue ₹10,234.50 lakh [C{hi}]; other income ₹198.70 lakh [C{lo}].\n")
    assert not g.ok and any(f"[C{hi}]" in b and "period_mismatch" in b for b in g.blocking)
    assert any(f"[C{lo}]" in w and "period_mismatch" in w for w in g.warnings)

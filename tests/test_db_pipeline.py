"""Integration tests against a real Postgres+pgvector test database (finresearch_test).

Skipped automatically when the database is unreachable. CI provides one via a service container.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from conftest import minimal_pdf

FIX = Path(__file__).parent / "fixtures"


def test_ingest_pdf_pages_lines_scanned_and_idempotent(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.ingest.documents import DocKind, get_or_create_company, ingest_pdf
    from finresearch.ingest.text import read_lines

    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(minimal_pdf([["Revenue from operations 11,716.54", "Profit for the year 535.61"], []]))
    with session_scope() as s:
        co = get_or_create_company(s, "acme", "Acme Ltd")
        doc = ingest_pdf(s, pdf, company=co, kind=DocKind.OTHER, docs_dir=env.docs_dir, ocr=True)
        assert doc.pages == 2 and doc.scanned_pages == 1
        pages = sorted(doc.page_rows, key=lambda p: p.page_no)
        lines = read_lines(doc.text_path)
        p1 = "\n".join(lines[pages[0].line_start - 1 : pages[0].line_end])
        assert "11,716.54" in p1 and pages[1].text_source == "tesseract"
        assert pages[0].line_end < pages[1].line_start
        again = ingest_pdf(s, pdf, company=co, kind=DocKind.OTHER, docs_dir=env.docs_dir)
        assert again.id == doc.id


@pytest.fixture
def table_doc(env, tmp_path):
    """A Document whose canonical text is the Orient P&L layout fixture (one page)."""
    from finresearch.db import session_scope
    from finresearch.db.models import Document, DocumentPage
    from finresearch.ingest.documents import get_or_create_company

    txt = tmp_path / "text.txt"
    shutil.copy(FIX / "orient_rhp_pnl_layout.txt", txt)
    n = len(txt.read_text().split("\n"))
    with session_scope() as s:
        co = get_or_create_company(s, "orient-cables", "Orient Cables (India) Limited")
        d = Document(company_id=co.id, kind="RHP", title="fixture", sha256=hashlib.sha256(str(tmp_path).encode()).hexdigest(), local_path=str(txt),
                     text_path=str(txt), bytes=1, pages=1)  # fmt: skip
        s.add(d)
        s.flush()
        s.add(DocumentPage(document_id=d.id, page_no=73, line_start=1, line_end=n, text="", char_count=0,
                           text_source="pdftotext"))  # fmt: skip
        return d.id


def call(name: str, args: dict) -> str:
    from finresearch.mcp_server.server import server

    r = asyncio.run(server.call_tool(name, args))
    return "\n".join(getattr(c, "text", "") for c in r.content)


def test_mcp_read_grep_table(table_doc):
    out = call("read_lines_tool", {"document_id": table_doc, "line_start": 1, "line_end": 12})
    assert "[page 73]" in out and "     8| " in out
    g = call("grep_document", {"document_id": table_doc, "pattern": r"Profit / \(Loss\) for the year"})
    assert "1 matching lines" in g and "p73" in g
    t = json.loads(call("extract_table", {"document_id": table_doc, "line_start": 1, "line_end": 39}))
    assert t["periods"][1] == "FY ended 2026-03-31" and "535.61" in t["markdown"]


def test_mcp_claims_quote_verification(table_doc):
    run = json.loads(call("start_run", {"company": "orient-cables"}))["run_id"]
    lines = (FIX / "orient_rhp_pnl_layout.txt").read_text().split("\n")
    ln = next(i for i, x in enumerate(lines, 1) if "Profit / (Loss) for the year" in x)
    ok = json.loads(call("save_claim", {
        "run_id": run, "stream": "financials", "statement": "FY26 PAT was Rs 535.61 mn", "claim_type": "numeric",
        "metric": "pat", "value": "535.61", "unit": "INR mn", "period": "FY2026", "importance": "high",
        "citations": [{"document_id": table_doc, "line_start": ln, "line_end": ln,
                       "quote": "Profit / (Loss) for the year 327.83 535.61"}]}))  # fmt: skip
    assert ok["status"] == "unverified" and ok["citation_checks"][0]["quote_found"] is True
    bad = json.loads(call("save_claim", {
        "run_id": run, "stream": "financials", "statement": "made up", "claim_type": "numeric", "metric": "pat", "unit": "INR mn", "period": "FY2026", "value": "999",
        "citations": [{"document_id": table_doc, "line_start": ln, "line_end": ln, "quote": "PAT was 999.99"}]}))  # fmt: skip
    assert bad["status"] == "unsupported"
    not_atomic = json.loads(call("save_claim", {
        "run_id": run, "stream": "financials", "statement": "PAT and revenue grew", "claim_type": "numeric",
        "value": "535.61", "citations": [{"document_id": table_doc, "line_start": ln, "line_end": ln,
                                          "quote": "Profit / (Loss) for the year 327.83 535.61"}]}))  # fmt: skip
    assert "atomic" in not_atomic["error"]
    err = json.loads(call("save_claim", {"run_id": run, "stream": "x", "statement": "y", "claim_type": "numeric",
                                         "citations": []}))  # fmt: skip
    assert "citation" in err["error"]
    listed = json.loads(call("list_claims", {"run_id": run}))
    assert [c["status"] for c in listed] == ["unverified", "unsupported"]


def test_mcp_fincalc_and_bad_args(env):
    r = json.loads(
        call(
            "fincalc_call",
            {"function": "ipo.allotment_probability_floor", "args": {"times_subscribed": "14.11"}},
        )
    )
    assert r["result"].startswith("0.0708")
    assert "signature" in call("fincalc_call", {"function": "growth.cagr", "args": {"nope": 1}})
    assert "unknown function" in call("fincalc_call", {"function": "os.system", "args": {}})


def test_mcp_save_claim_with_a_bad_citation_saves_nothing(table_doc):
    """Regression: the claim row was inserted before its citations were checked, so a bad citation left a
    half-saved claim behind and the agent's corrected retry duplicated it."""
    run = json.loads(call("start_run", {"company": "orient-cables"}))["run_id"]
    base = {"run_id": run, "stream": "financials", "statement": "FY26 PAT was Rs 535.61 mn", "claim_type": "numeric",
            "metric": "pat", "value": "535.61", "unit": "INR mn", "period": "FY2026"}  # fmt: skip
    good = {"document_id": table_doc, "line_start": 1, "line_end": 1, "quote": "x"}
    for bad in ({"document_id": 99999999, "line_start": 1, "quote": "x"},
                {"url": "https://x.example", "accessed_at": "yesterday", "quote": "x"},
                {"document_id": table_doc, "quote": "no line"}, {"quote": "neither"}):  # fmt: skip
        err = json.loads(call("save_claim", {**base, "citations": [good, bad]}))
        assert "error" in err, err
    assert json.loads(call("list_claims", {"run_id": run})) == []

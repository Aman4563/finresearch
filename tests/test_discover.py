"""Document discovery: matching, classification, candidate validation, archive handling and ingestion."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import respx
from conftest import minimal_pdf

from finresearch.adapters.nse import parse_issue_info
from finresearch.ingest.discover import (
    Candidate,
    _pdfs_from,
    agent_candidates,
    fetch_and_ingest,
    kind_from_title,
    names_match,
    nse_candidates,
)
from finresearch.ingest.documents import DocKind

FIX = Path(__file__).parent / "fixtures"


def test_names_match_ignores_legal_suffixes():
    assert names_match("Orient Cables (India) Limited", "ORIENT CABLES (INDIA) LTD")
    assert names_match("Moneyview Limited", "Moneyview Ltd.")
    assert not names_match("Orient Cables (India) Limited", "Orient Electric Limited")


def test_kind_from_title():
    assert kind_from_title("Orient Cables (India) Limited - RHP") is DocKind.RHP
    assert kind_from_title("Moneyview Limited - UDRHP-I") is DocKind.DRHP
    assert kind_from_title("Addendum to DRHP") is DocKind.ADDENDUM
    assert kind_from_title("Annual Report 2025-26") is DocKind.ANNUAL_REPORT
    assert kind_from_title("Audited Standalone Financial Statements FY25") is DocKind.FINANCIALS
    assert kind_from_title("Price Band Advertisement") is DocKind.PRICE_BAND_AD


def test_nse_candidates_from_real_issue_info():
    info = parse_issue_info(
        json.loads((FIX / "nse" / "ipo_detail_ORIENTCABL_20260928_1636_live.json").read_text())["issueInfo"]
    )
    got = {c.kind: c.url for c in nse_candidates(info)}
    assert got[DocKind.RHP].endswith("RHP_ORIENTCABL.zip") and got[DocKind.ANCHOR].endswith(
        "ANCHOR_ORIENTCABL.zip"
    )


def test_agent_candidates_rejects_non_http_and_keeps_provenance():
    res = {"documents": [
        {"url": "https://orientcables.in/wp-content/uploads/2026/09/AR-2025-26-1.pdf", "kind": "ANNUAL_REPORT",
         "title": "AR 2025-26", "found_on": "https://orientcables.in/annual-reports/"},
        {"url": "javascript:alert(1)", "kind": "OTHER", "title": "x", "found_on": "y"},
        {"url": "/relative.pdf", "kind": "OTHER", "title": "rel", "found_on": "y"},
        {"url": "https://x.example/fs.pdf", "kind": "not-a-kind", "title": "Audited Financial FY24", "found_on": "p"},
    ]}  # fmt: skip
    c = agent_candidates(res)
    assert [x.kind for x in c] == [DocKind.ANNUAL_REPORT, DocKind.FINANCIALS]
    assert c[0].referer == "https://orientcables.in/annual-reports/" and c[0].source == "agent"


def test_pdfs_from_zip_and_pdf(tmp_path):
    pdf = minimal_pdf([["hello"]])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("RHP_X.pdf", pdf)
        z.writestr("readme.txt", "x")
    (tmp_path / "a.zip").write_bytes(buf.getvalue())
    (tmp_path / "b.bin").write_bytes(pdf)
    (tmp_path / "c.html").write_bytes(b"<html>blocked</html>")
    assert [n for n, _ in _pdfs_from(tmp_path / "a.zip", "u")] == ["RHP_X.pdf"]
    assert len(_pdfs_from(tmp_path / "b.bin", "https://h/x.pdf")) == 1
    assert _pdfs_from(tmp_path / "c.html", "u") == []


@respx.mock
def test_fetch_and_ingest_dedupes_and_records_failures(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Document
    from finresearch.ingest.documents import get_or_create_company

    pdf = minimal_pdf([["Annual report text 11,716.54"]])
    respx.get("https://co.example/ar.pdf").mock(return_value=httpx.Response(200, content=pdf))
    respx.get("https://mirror.example/ar-copy.pdf").mock(return_value=httpx.Response(200, content=pdf))
    respx.get("https://co.example/blocked.pdf").mock(
        return_value=httpx.Response(200, content=b"<html>login</html>")
    )
    respx.get("https://co.example/missing.pdf").mock(return_value=httpx.Response(404))
    cands = [Candidate("https://co.example/ar.pdf", DocKind.ANNUAL_REPORT, "AR FY26", "agent", "https://co.example/ir"),
             Candidate("https://mirror.example/ar-copy.pdf", DocKind.ANNUAL_REPORT, "AR copy", "agent"),
             Candidate("https://co.example/ar.pdf", DocKind.ANNUAL_REPORT, "same url", "agent"),
             Candidate("https://co.example/blocked.pdf", DocKind.OTHER, "blocked", "agent"),
             Candidate("https://co.example/missing.pdf", DocKind.OTHER, "missing", "agent")]  # fmt: skip
    with session_scope() as s:
        co = get_or_create_company(s, "disc-" + tmp_path.name[-8:], "Disc Co")
        rep = fetch_and_ingest(s, co, cands, docs_dir=env.docs_dir, index=False)
        statuses = [o.status for o in rep.outcomes]
        assert statuses == ["ingested", "duplicate", "skipped", "failed", "failed"]
        doc = s.get(Document, rep.outcomes[0].document_id)
        assert doc.kind == "ANNUAL_REPORT" and doc.provenance["discovered_via"] == "agent"
        assert (
            doc.provenance["found_on"] == "https://co.example/ir"
            and doc.source_url == "https://co.example/ar.pdf"
        )


@respx.mock
async def test_discover_indexes_documents_from_inside_an_event_loop(env, tmp_path, monkeypatch):
    """Regression: discover() is async but indexing calls asyncio.run() for embeddings; it crashed with
    'asyncio.run() cannot be called from a running event loop' on its first live run."""
    from finresearch.db import session_scope
    from finresearch.db.models import Chunk, Document
    from finresearch.ingest import discover as disc
    from finresearch.ingest.documents import get_or_create_company

    class FakeEmbedder:
        embed_model = "fake"

        async def embed(self, texts):
            return [[0.0] * 1024 for _ in texts]

    from finresearch.bridge import Tier

    class FakeRouter:
        def __init__(self):
            self.engines = {Tier.LOCAL: FakeEmbedder()}

    slug = "loop-" + tmp_path.name[-8:]
    with session_scope() as s:
        get_or_create_company(s, slug, "Loop Co")

    async def fake_sebi(name, **kw):
        return [Candidate("https://sebi.example/rhp.pdf", DocKind.RHP, "Loop Co - RHP", "sebi")]

    monkeypatch.setattr(disc, "sebi_candidates", fake_sebi)
    monkeypatch.setattr("finresearch.bridge.build_router", lambda *a, **k: FakeRouter())
    respx.get("https://sebi.example/rhp.pdf").mock(
        return_value=httpx.Response(200, content=minimal_pdf([["RISK FACTORS"]]))
    )
    rep = await disc.discover(slug, use_agent=False, index=True, log=lambda m: None)
    assert [o.status for o in rep.outcomes] == ["ingested"]
    with session_scope() as s:
        doc = s.get(Document, rep.outcomes[0].document_id)
        assert doc.kind == "RHP" and s.query(Chunk).filter_by(document_id=doc.id).count() > 0


def test_zip_members_are_classified_by_their_own_names():
    from finresearch.ingest.discover import kind_for_member

    assert kind_for_member("ACEVECTOR LIMITED - GID.pdf", DocKind.RHP) == DocKind.OTHER
    assert kind_for_member("Orient_GID.pdf", DocKind.RHP) == DocKind.OTHER  # '_' is a word character
    assert kind_for_member("Rigid_Plastics_RHP.pdf", DocKind.RHP) == DocKind.RHP
    assert kind_for_member("Acevector_RHP.pdf", DocKind.RHP) == DocKind.RHP
    assert kind_for_member("Abridged Prospectus.pdf", DocKind.RHP) == DocKind.ABRIDGED


@respx.mock
def test_a_broken_document_fails_alone_and_earlier_documents_are_kept(env, tmp_path, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import Document
    from finresearch.ingest import discover as disc
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.ingest.documents import ingest_pdf as real_ingest

    good, bad = minimal_pdf([["Annual report FY26"]]), minimal_pdf([["Broken scan"]])
    respx.get("https://co.example/good.pdf").mock(return_value=httpx.Response(200, content=good))
    respx.get("https://co.example/bad.pdf").mock(return_value=httpx.Response(200, content=bad))

    def flaky_ingest(session, pdf, **kw):
        if kw["title"] == "bad":
            raise RuntimeError("OCR crashed")
        return real_ingest(session, pdf, **kw)

    monkeypatch.setattr(disc, "ingest_pdf", flaky_ingest)
    cands = [Candidate("https://co.example/good.pdf", DocKind.ANNUAL_REPORT, "good", "agent"),
             Candidate("https://co.example/bad.pdf", DocKind.ANNUAL_REPORT, "bad", "agent")]  # fmt: skip
    slug = "flaky-" + tmp_path.name[-8:]
    with session_scope() as s:
        co = get_or_create_company(s, slug, "Flaky Co")
        rep = fetch_and_ingest(s, co, cands, docs_dir=env.docs_dir, index=False)
    assert [o.status for o in rep.outcomes] == ["ingested", "failed"] and "OCR crashed" in rep.outcomes[
        1
    ].detail
    with session_scope() as s:
        assert s.get(Document, rep.outcomes[0].document_id).title == "good"


@respx.mock
def test_a_byte_different_copy_with_the_same_text_is_a_duplicate(env, tmp_path):
    """Live: NSE's copy of the Orient Cables RHP differs in bytes from SEBI's but 480/491 pages are identical."""
    from finresearch.db import session_scope
    from finresearch.db.models import Document
    from finresearch.ingest.documents import get_or_create_company

    body = [[f"Page {i} of the offer document with restated financials"] for i in range(1, 11)]
    signed_a = minimal_pdf([*body, ["Signed by director A"]])
    signed_b = minimal_pdf([*body, ["Signed by director B"]])
    other = minimal_pdf([[f"Annual report page {i}"] for i in range(1, 12)])
    respx.get("https://sebi.example/rhp.pdf").mock(return_value=httpx.Response(200, content=signed_a))
    respx.get("https://nse.example/rhp.pdf").mock(return_value=httpx.Response(200, content=signed_b))
    respx.get("https://co.example/ar.pdf").mock(return_value=httpx.Response(200, content=other))
    cands = [Candidate("https://sebi.example/rhp.pdf", DocKind.RHP, "SEBI RHP", "sebi"),
             Candidate("https://nse.example/rhp.pdf", DocKind.RHP, "NSE RHP", "nse"),
             Candidate("https://co.example/ar.pdf", DocKind.ANNUAL_REPORT, "AR", "agent")]  # fmt: skip
    with session_scope() as s:
        co = get_or_create_company(s, "twin-" + tmp_path.name[-8:], "Twin Co")
        rep = fetch_and_ingest(s, co, cands, docs_dir=env.docs_dir, index=False)
        first = rep.outcomes[0].document_id
        assert [o.status for o in rep.outcomes] == ["ingested", "duplicate", "ingested"]
        assert rep.outcomes[1].document_id == first and "same text" in rep.outcomes[1].detail
        assert s.query(Document).filter_by(company_id=co.id).count() == 2

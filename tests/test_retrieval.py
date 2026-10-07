"""Retrieval (#248): the section mapper, the chunker and hybrid search, plus a small retrieval-quality eval (Recall@k
and MRR) on a recorded fixture document, and keyword-only indexing when embedding fails.

The fixture (tests/fixtures/retrieval/synthetic_rhp.txt) is a synthetic offer document in the SEBI ICDR layout for a
made-up company: 15 pages separated by form feeds, a table of contents with dotted leaders, a summary that repeats a
later heading as a sub-heading, and a cross-reference that repeats a heading mid-section. gold_queries.json lists
each query with the line that answers it (checked below to contain the answer's key phrase)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from pathlib import Path

from finresearch.ingest.index import CHUNK_MAX_LINES, CHUNK_OVERLAP_LINES, make_chunks
from finresearch.ingest.sections import map_sections
from finresearch.ingest.text import read_lines

FIX = Path(__file__).parent / "fixtures" / "retrieval"
DOC = FIX / "synthetic_rhp.txt"
GOLD = json.loads((FIX / "gold_queries.json").read_text())

# (canonical, first line, last line), read off `grep -n` of the fixture's stand-alone heading lines: each section
# runs to the line before the next heading, the last to the end of the file (281 lines)
EXPECTED_SECTIONS = [
    ("DEFINITIONS", 26, 42), ("OFFER_DOCUMENT_SUMMARY", 43, 63), ("RISK_FACTORS", 64, 87), ("THE_OFFER", 88, 104),
    ("CAPITAL_STRUCTURE", 105, 121), ("OBJECTS_OF_THE_OFFER", 122, 145), ("BASIS_FOR_OFFER_PRICE", 146, 162),
    ("OUR_BUSINESS", 163, 189), ("OUR_MANAGEMENT", 190, 206), ("PROMOTERS", 207, 223),
    ("RESTATED_FINANCIALS", 224, 247), ("LITIGATION", 248, 264), ("OFFER_PROCEDURE", 265, 281),
]  # fmt: skip


# --------------------------------------------------------------------------- map_sections
def test_map_sections_on_the_fixture():
    """TOC lines (dotted leaders) are skipped; the summary's "OBJECTS OF THE OFFER" (line 46) and the
    cross-reference "OUR MANAGEMENT" after a non-blank line (188) do not start sections."""
    lines = read_lines(DOC)
    assert len(lines) == 281
    got = [(s.canonical, s.line_start, s.line_end) for s in map_sections(lines)]
    assert got == EXPECTED_SECTIONS


def test_map_sections_without_risk_factors_or_headings():
    assert map_sections(["just a letter", "", "with no headings at all"]) == []
    lines = ["", "OUR BUSINESS", "", "We make pumps.", "", "OUR MANAGEMENT", "", "Directors."]
    assert [(s.canonical, s.line_start, s.line_end) for s in map_sections(lines)] == [
        ("OUR_BUSINESS", 2, 5),
        ("OUR_MANAGEMENT", 6, 8),
    ]


def test_every_gold_line_is_in_its_section_and_holds_the_answer():
    lines = read_lines(DOC)
    spans = {c: (a, b) for c, a, b in EXPECTED_SECTIONS}
    assert len(GOLD) >= 10
    for g in GOLD:
        a, b = spans[g["section"]]
        assert a <= g["line"] <= b and g["key"] in lines[g["line"] - 1], g


# --------------------------------------------------------------------------- make_chunks
def test_make_chunks_windows_overlap_and_cover_every_line():
    lines = [f"line {i}" for i in range(1, 101)]
    drafts = make_chunks(lines, [(7, 1, 100)])
    # 40-line windows stepping 36 (40 - 4 overlap): 1-40, 37-76, 73-100
    assert [(d.line_start, d.line_end) for d in drafts] == [(1, 40), (37, 76), (73, 100)]
    assert CHUNK_MAX_LINES == 40 and CHUNK_OVERLAP_LINES == 4
    assert all(d.section_id == 7 for d in drafts)
    assert drafts[1].text.splitlines()[0] == "line 37" and drafts[1].text.splitlines()[-1] == "line 76"


def test_make_chunks_char_cap_giant_line_blank_and_form_feed():
    lines = ["a" * 1000, "b" * 1000, "c" * 1000, "\f", "   ", "x" * 5000, "\fend"]
    drafts = make_chunks(lines, [(None, 1, 7)])
    spans = [(d.line_start, d.line_end) for d in drafts]
    # 2,400 characters: two 1,000-character lines fit (2,002 with newlines), a third does not
    assert spans[0] == (1, 2)
    giant = next(d for d in drafts if "x" * 5000 in d.text)  # a line longer than the cap is still one chunk
    assert (giant.line_start, giant.line_end) == (6, 6)
    assert all("\f" not in d.text for d in drafts) and drafts[-1].text.endswith("end")
    assert not any(d.text.strip() == "" for d in drafts)  # a blank-only window is not stored
    covered = {n for d in drafts for n in range(d.line_start, d.line_end + 1)}
    assert {1, 2, 3, 6, 7} <= covered


def test_make_chunks_respects_ranges():
    lines = [f"l{i}" for i in range(1, 21)]
    drafts = make_chunks(lines, [(None, 1, 4), (1, 5, 12), (2, 13, 20)])
    assert [(d.section_id, d.line_start, d.line_end) for d in drafts] == [
        (None, 1, 4),
        (1, 5, 12),
        (2, 13, 20),
    ]


# --------------------------------------------------------------------------- hybrid search and the eval
class HashEmbedder:
    """A deterministic stand-in for the local embedding model: hashed bag of words (unit length, EMBED_DIM)."""

    embed_model = "test-hash-bow"

    def __init__(self, fail_after: int | None = None):
        self.calls, self.fail_after = 0, fail_after

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from finresearch.db.models import EMBED_DIM

        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ConnectionError("ollama is not running")
        out = []
        for t in texts:
            v = [0.0] * EMBED_DIM
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.sha1(w.encode()).hexdigest(), 16) % EMBED_DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def _store(session, tmp_path, slug: str):
    """The fixture as a stored document: text file, one DocumentPage per form-feed page, kind RHP."""
    from finresearch.db.models import Document, DocumentPage
    from finresearch.ingest.documents import get_or_create_company

    text_path = tmp_path / f"{slug}.txt"
    shutil.copy(DOC, text_path)
    co = get_or_create_company(session, slug, "Example Pumps and Valves Ltd (synthetic)")
    doc = Document(company_id=co.id, kind="RHP", title=f"Synthetic RHP {slug}", sha256=hashlib.sha256(slug.encode()).hexdigest(),
                   local_path=str(text_path), text_path=str(text_path), bytes=DOC.stat().st_size, pages=15, provenance={})  # fmt: skip
    session.add(doc)
    session.flush()
    lines, page, start = read_lines(text_path), 1, 1
    for i, ln in enumerate(lines, 1):
        if "\f" in ln and i > 1:
            session.add(DocumentPage(document_id=doc.id, page_no=page, line_start=start, line_end=i - 1, text="",
                                     char_count=0, text_source="pdftotext"))  # fmt: skip
            page, start = page + 1, i
    session.add(DocumentPage(document_id=doc.id, page_no=page, line_start=start, line_end=len(lines), text="",
                             char_count=0, text_source="pdftotext"))  # fmt: skip
    session.flush()
    assert page == 15
    return doc


def _rank(hits, line: int) -> int | None:
    """1-based rank of the first hit whose line span covers the gold line."""
    return next((r for r, h in enumerate(hits, 1) if h.line_start <= line <= h.line_end), None)


def evaluate(session, doc_id: int, embedder=None, k: int = 5) -> dict[str, float]:
    """Recall@k (share of gold queries with a covering chunk in the top k) and MRR over the top k."""
    from finresearch.ingest.index import hybrid_search

    ranks = [_rank(hybrid_search(session, g["query"], document_ids=[doc_id], embedder=embedder, k=k), g["line"])
             for g in GOLD]  # fmt: skip
    return {"recall": sum(r is not None for r in ranks) / len(ranks),
            "mrr": sum(1 / r for r in ranks if r is not None) / len(ranks), "ranks": ranks}  # fmt: skip


def test_hybrid_search_full_text_returns_cited_line_spans(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.ingest.index import hybrid_search, index_document

    with session_scope() as s:
        doc = _store(s, tmp_path, "retr-fts-" + tmp_path.name[-6:])
        n = index_document(s, doc)
        assert n > 0 and doc.provenance["index"] == {"mode": "keyword_only", "reason": "no embedder"}
        hits = hybrid_search(s, "capacity utilisation", document_ids=[doc.id], k=3)
        top = hits[0]
        assert top.section == "OUR_BUSINESS" and top.document_title.startswith("Synthetic RHP")
        assert top.line_start <= 171 <= top.line_end and "capacity utilisation of 78%" in top.text
        assert top.page_start is not None and top.page_start <= top.page_end
        assert (
            hybrid_search(s, "zeppelin hovercraft", document_ids=[doc.id]) == []
        )  # no match: nothing invented
        assert (
            hybrid_search(s, "capacity utilisation", document_ids=[-1]) == []
        )  # scoped to the documents given


def test_retrieval_eval_recall_and_mrr(env, tmp_path, capsys):
    """The eval on the gold set. Floors are this fixture's measured scores rounded down, so a change to the chunker,
    the mapper or the ranking that loses gold answers fails here."""
    from finresearch.db import session_scope
    from finresearch.ingest.index import index_document

    with session_scope() as s:
        doc = _store(s, tmp_path, "retr-eval-" + tmp_path.name[-6:])
        index_document(s, doc, embedder=HashEmbedder())
        assert doc.provenance["index"]["mode"] == "hybrid"
        fts = evaluate(s, doc.id)
        hybrid = evaluate(s, doc.id, embedder=HashEmbedder())
    with capsys.disabled():
        print(f"\nretrieval eval ({len(GOLD)} gold queries, k=5): full-text Recall@5 {fts['recall']:.2f} "
              f"MRR {fts['mrr']:.2f}; hybrid Recall@5 {hybrid['recall']:.2f} MRR {hybrid['mrr']:.2f}")  # fmt: skip
    assert fts["recall"] >= FLOORS["fts_recall"] and fts["mrr"] >= FLOORS["fts_mrr"], fts
    assert hybrid["recall"] >= FLOORS["hybrid_recall"] and hybrid["mrr"] >= FLOORS["hybrid_mrr"], hybrid


# Measured 2026-10-07 (16 queries, k=5): full-text Recall@5 0.50, MRR 0.47; hybrid with the hashed bag-of-words
# stand-in embedder Recall@5 0.94, MRR 0.71. Full text alone misses questions phrased with words the answer does not
# use ("Who is the book running lead manager?"): websearch_to_tsquery ANDs every term, so the vector half carries
# those. The hybrid figure measures the fusion, not the real embedding model (which needs Ollama, not in CI).
FLOORS = {"fts_recall": 0.5, "fts_mrr": 0.45, "hybrid_recall": 0.9, "hybrid_mrr": 0.7}


def test_embedding_failure_indexes_keyword_only_instead_of_dropping(env, tmp_path):
    """#248 (verification B, claim 9): index_document embedded with no try, and discovery rolled the whole document
    back on any error, so an embedding outage lost the document. Now it is stored keyword-only and still found."""
    from finresearch.db import session_scope
    from finresearch.db.models import Chunk
    from finresearch.ingest.index import hybrid_search, index_document

    with session_scope() as s:
        doc = _store(s, tmp_path, "retr-fail-" + tmp_path.name[-6:])
        emb = HashEmbedder(fail_after=1)  # the first batch embeds, the second fails
        n = index_document(s, doc, embedder=emb, batch=4)
        assert emb.calls == 2 and n > 4
        chunks = s.query(Chunk).filter(Chunk.document_id == doc.id).all()
        assert len(chunks) == n and all(c.embedding is None and c.embed_model is None for c in chunks)
        assert doc.provenance["index"]["mode"] == "keyword_only"
        assert "ConnectionError: ollama is not running" in doc.provenance["index"]["reason"]
        hits = hybrid_search(s, "authorised share capital", document_ids=[doc.id])
        assert hits and hits[0].section == "CAPITAL_STRUCTURE"

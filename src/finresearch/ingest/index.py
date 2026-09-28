"""Sections + chunks + embeddings for a Document, and hybrid (full-text + vector) search with citations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from finresearch.db.models import Chunk, Document, DocumentPage, Section
from finresearch.ingest.sections import map_sections, page_for_line
from finresearch.ingest.text import read_lines

CHUNK_MAX_LINES = 40
CHUNK_MAX_CHARS = 2400
CHUNK_OVERLAP_LINES = 4
QUERY_INSTRUCTION = (
    "Instruct: Given a question about a company's offer document, retrieve passages that answer it\nQuery: "
)
OFFER_DOC_KINDS = {"RHP", "DRHP", "ABRIDGED_PROSPECTUS", "ADDENDUM"}


def page_spans(session: Session, doc: Document) -> list[tuple[int, int, int]]:
    rows = session.execute(
        select(DocumentPage.page_no, DocumentPage.line_start, DocumentPage.line_end)
        .where(DocumentPage.document_id == doc.id)
        .order_by(DocumentPage.page_no)
    ).all()
    return [tuple(r) for r in rows]


def build_sections(session: Session, doc: Document) -> list[Section]:
    session.execute(delete(Section).where(Section.document_id == doc.id))
    if doc.kind not in OFFER_DOC_KINDS:
        return []
    lines = read_lines(doc.text_path)
    spans = page_spans(session, doc)
    out = []
    for sp in map_sections(lines):
        sec = Section(
            document_id=doc.id,
            canonical=sp.canonical,
            heading=sp.heading[:300],
            line_start=sp.line_start,
            line_end=sp.line_end,
            page_start=page_for_line(sp.line_start, spans),
            page_end=page_for_line(sp.line_end, spans),
        )
        session.add(sec)
        out.append(sec)
    session.flush()
    return out


@dataclass
class ChunkDraft:
    section_id: int | None
    line_start: int
    line_end: int
    text: str


def make_chunks(lines: list[str], ranges: list[tuple[int | None, int, int]]) -> list[ChunkDraft]:
    """ranges: (section_id, first_line, last_line) 1-based inclusive. Windows respect line boundaries."""
    drafts: list[ChunkDraft] = []
    for sid, a, b in ranges:
        i = a
        while i <= b:
            j, size = i, 0
            while j <= b and j - i < CHUNK_MAX_LINES and size + len(lines[j - 1]) <= CHUNK_MAX_CHARS:
                size += len(lines[j - 1]) + 1
                j += 1
            j = max(j, i + 1)  # always progress, even on a giant line
            body = "\n".join(lines[i - 1 : j - 1]).replace("\f", "")
            if body.strip():
                drafts.append(ChunkDraft(sid, i, j - 1, body))
            if j > b:
                break
            i = max(j - CHUNK_OVERLAP_LINES, i + 1)
    return drafts


def index_document(session: Session, doc: Document, *, embedder=None, batch: int = 32, progress=None) -> int:
    """(Re)build sections and chunks; embed with the local embedding model if given. Returns chunk count."""
    sections = build_sections(session, doc)
    session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    lines = read_lines(doc.text_path)
    spans = page_spans(session, doc)
    ranges: list[tuple[int | None, int, int]] = []
    if sections:
        first = min(s.line_start for s in sections)
        if first > 1:
            ranges.append((None, 1, first - 1))
        ranges += [(s.id, s.line_start, s.line_end) for s in sections]
    else:
        ranges.append((None, 1, len(lines)))
    drafts = make_chunks(lines, ranges)

    vectors: list[list[float] | None] = [None] * len(drafts)
    model = None
    if embedder is not None:
        model = embedder.embed_model
        for k in range(0, len(drafts), batch):
            vecs = asyncio.run(embedder.embed([d.text for d in drafts[k : k + batch]]))
            vectors[k : k + batch] = vecs
            if progress:
                progress(f"embedded {min(k + batch, len(drafts))}/{len(drafts)}")
    for d, v in zip(drafts, vectors, strict=True):
        session.add(
            Chunk(
                document_id=doc.id,
                section_id=d.section_id,
                line_start=d.line_start,
                line_end=d.line_end,
                page_start=page_for_line(d.line_start, spans),
                page_end=page_for_line(d.line_end, spans),
                text=d.text,
                embedding=v,
                embed_model=model if v is not None else None,
            )
        )
    session.flush()
    return len(drafts)


# --------------------------------------------------------------------------- search
@dataclass
class Hit:
    chunk_id: int
    document_id: int
    document_title: str
    section: str | None
    page_start: int | None
    page_end: int | None
    line_start: int
    line_end: int
    score: float
    text: str


def hybrid_search(
    session: Session,
    query: str,
    *,
    document_ids: list[int] | None = None,
    embedder=None,
    k: int = 8,
    pool: int = 50,
    rrf_k: int = 60,
) -> list[Hit]:
    """Reciprocal-rank fusion of Postgres full-text rank and pgvector cosine similarity."""
    scope = "AND c.document_id = ANY(:docs)" if document_ids else ""
    params: dict = {"q": query, "pool": pool, "docs": document_ids or []}
    fts = (
        session.execute(
            text(f"""SELECT c.id FROM chunk c WHERE c.tsv @@ websearch_to_tsquery('english', :q) {scope}
                 ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) DESC LIMIT :pool"""),
            params,
        )
        .scalars()
        .all()
    )
    vec: list[int] = []
    if embedder is not None:
        qv = asyncio.run(embedder.embed([QUERY_INSTRUCTION + query]))[0]
        vec = (
            session.execute(
                text(f"""SELECT c.id FROM chunk c WHERE c.embedding IS NOT NULL {scope}
                     ORDER BY c.embedding <=> CAST(:qv AS vector) LIMIT :pool"""),
                {**params, "qv": str(qv)},
            )
            .scalars()
            .all()
        )
    scores: dict[int, float] = {}
    for ranking in (fts, vec):
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
    top = sorted(scores, key=scores.get, reverse=True)[:k]
    if not top:
        return []
    rows = session.execute(
        select(Chunk, Document.title, Section.canonical)
        .join(Document, Document.id == Chunk.document_id)
        .outerjoin(Section, Section.id == Chunk.section_id)
        .where(Chunk.id.in_(top))
    ).all()
    by_id = {c.id: (c, title, canon) for c, title, canon in rows}
    return [
        Hit(
            cid,
            by_id[cid][0].document_id,
            by_id[cid][1],
            by_id[cid][2],
            by_id[cid][0].page_start,
            by_id[cid][0].page_end,
            by_id[cid][0].line_start,
            by_id[cid][0].line_end,
            round(scores[cid], 5),
            by_id[cid][0].text,
        )
        for cid in top
    ]

"""Discover and ingest a company's offer and investor-relations documents.

Sources, most deterministic first:
1. NSE issue information: links to the RHP and the anchor-allocation report (ZIP archives of PDFs).
2. SEBI public-issue filings: DRHP / RHP / prospectus / addenda, matched by company name.
3. A discovery agent (web tools only) for company IR pages: annual reports, audited statements, price-band ad,
   industry report. It may only return URLs it actually saw; Python validates, downloads and ingests them.

Every document is validated (PDF magic bytes, size cap), deduplicated by sha256 and stored with provenance.
"""

from __future__ import annotations

import io
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from finresearch.db.models import Company
from finresearch.ingest.documents import DocKind, download, ingest_pdf

MAX_BYTES = 150 * 1024 * 1024
NSE_ZIP_KINDS = {"Red Herring Prospectus": DocKind.RHP, "Anchor Allocation Report": DocKind.ANCHOR}
_STOP = {"limited", "ltd", "private", "pvt", "india", "the", "and", "&", "co", "company", "corporation"}


@dataclass
class Candidate:
    url: str
    kind: DocKind
    title: str
    source: str  # nse | sebi | agent
    referer: str | None = None


@dataclass
class Outcome:
    candidate: Candidate
    status: str  # ingested | duplicate | failed | skipped
    document_id: int | None = None
    detail: str | None = None


@dataclass
class DiscoveryReport:
    outcomes: list[Outcome] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


# --------------------------------------------------------------------------- matching & classification
def name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in _STOP and len(t) > 1}


def names_match(a: str, b: str) -> bool:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta or len(ta & tb) / min(len(ta), len(tb)) >= 0.8


def kind_for_member(member: str, default: DocKind) -> DocKind:
    """Classify one PDF inside an exchange ZIP; NSE bundles the General Information Document with the RHP."""
    t = Path(member).stem.lower()
    if re.search(r"\bgid\b|general[ _-]*information", t):
        return DocKind.OTHER
    if "abridged" in t:
        return DocKind.ABRIDGED
    return default


def kind_from_title(title: str) -> DocKind:
    t = title.lower()
    if "abridged" in t:
        return DocKind.ABRIDGED
    if "addendum" in t or "corrigendum" in t:
        return DocKind.ADDENDUM
    if re.search(r"\bu?drhp\b|draft red herring", t):
        return DocKind.DRHP
    if re.search(r"\brhp\b|red herring", t):
        return DocKind.RHP
    if "prospectus" in t:
        return DocKind.RHP
    if "annual report" in t:
        return DocKind.ANNUAL_REPORT
    if re.search(r"financial statement|audited|restated|results", t):
        return DocKind.FINANCIALS
    if "price band" in t:
        return DocKind.PRICE_BAND_AD
    if "anchor" in t:
        return DocKind.ANCHOR
    if "industry" in t:
        return DocKind.INDUSTRY_REPORT
    return DocKind.OTHER


# --------------------------------------------------------------------------- sources
def nse_candidates(issue_info: dict[str, str]) -> list[Candidate]:
    out = []
    for key, kind in NSE_ZIP_KINDS.items():
        url = (issue_info.get(key) or "").strip()
        if url.startswith("http"):
            out.append(Candidate(url, kind, f"NSE {key}", "nse", "https://www.nseindia.com/"))
    return out


async def sebi_candidates(company_name: str, *, pages: int = 3, client=None) -> list[Candidate]:
    from finresearch.adapters.sebi import SebiClient

    out: list[Candidate] = []
    async with client or SebiClient() as sebi:
        for kind in ("drhp", "rhp", "prospectus"):
            try:
                filings = await sebi.list_public_issue_filings(kind=kind, pages=pages)
            except Exception:
                continue
            for f in filings:
                if not names_match(company_name, f.company):
                    continue
                try:
                    pdfs = await sebi.resolve_pdf_url(f.detail_url)
                except Exception:
                    continue
                if pdfs:
                    out.append(Candidate(pdfs[0], kind_from_title(f.title), f.title, "sebi", f.detail_url))
    return out


def agent_candidates(result: dict[str, Any]) -> list[Candidate]:
    """Validate the discovery agent's structured output into candidates (http(s) PDFs only)."""
    out = []
    for d in result.get("documents", []):
        url = str(d.get("url", "")).strip()
        if not re.match(r"^https?://", url):
            continue
        kind = (
            DocKind(d["kind"])
            if d.get("kind") in DocKind.__members__.values()
            else kind_from_title(d.get("title", ""))
        )
        out.append(Candidate(url, kind, d.get("title") or url.rsplit("/", 1)[-1], "agent", d.get("found_on")))
    return out


# --------------------------------------------------------------------------- download & ingest
def _pdfs_from(path: Path, url: str) -> list[tuple[str, bytes]]:
    data = path.read_bytes()
    if data[:5] == b"%PDF-":
        return [(url.rsplit("/", 1)[-1] or "document.pdf", data)]
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return [(n, z.read(n)) for n in z.namelist() if n.lower().endswith(".pdf")]
    return []


def fetch_and_ingest(session: Session, company: Company, cands: list[Candidate], *, docs_dir: Path,
                     embedder=None, index: bool = True, glm_ocr=None) -> DiscoveryReport:  # fmt: skip
    from finresearch.db.models import Document
    from finresearch.ingest.index import index_document

    report = DiscoveryReport()
    seen_urls: set[str] = set()
    for c in cands:
        if c.url in seen_urls:
            report.outcomes.append(Outcome(c, "skipped", detail="duplicate URL"))
            continue
        seen_urls.add(c.url)
        with tempfile.TemporaryDirectory() as td:
            try:
                path, prov = download(c.url, Path(td), referer=c.referer)
            except Exception as e:
                report.outcomes.append(Outcome(c, "failed", detail=f"download: {e}"[:300]))
                continue
            if path.stat().st_size > MAX_BYTES:
                report.outcomes.append(Outcome(c, "failed", detail="file larger than 150 MB"))
                continue
            pdfs = _pdfs_from(path, c.url)
            if not pdfs:
                report.outcomes.append(Outcome(c, "failed", detail="not a PDF or a ZIP of PDFs"))
                continue
            for name, data in pdfs:
                pdf = Path(td) / re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)
                pdf.write_bytes(data)
                before = session.query(Document.id).filter(Document.sha256.isnot(None)).count()
                try:
                    doc = ingest_pdf(session, pdf, company=company, kind=kind_for_member(name, c.kind),
                                     title=f"{c.title} — {Path(name).stem}" if len(pdfs) > 1 else c.title,
                                     docs_dir=docs_dir, glm_ocr=glm_ocr,
                                     provenance={**prov, "discovered_via": c.source, "found_on": c.referer})  # fmt: skip
                    after = session.query(Document.id).filter(Document.sha256.isnot(None)).count()
                    if after == before:
                        report.outcomes.append(Outcome(c, "duplicate", doc.id, "already in the store"))
                        continue
                    if index:
                        index_document(session, doc, embedder=embedder)
                    session.commit()  # keep each finished document even if a later download or OCR fails
                except Exception as e:
                    session.rollback()
                    report.outcomes.append(Outcome(c, "failed", detail=f"ingest {name}: {e}"[:300]))
                    continue
                report.outcomes.append(Outcome(c, "ingested", doc.id, f"{doc.pages} pages"))
    return report


# --------------------------------------------------------------------------- orchestration
async def discover(
    company_slug: str, *, use_agent: bool = True, index: bool = True, log=print
) -> DiscoveryReport:
    """Find, download and ingest a company's documents from NSE, SEBI and (optionally) its IR pages."""
    import asyncio
    import json

    from finresearch.agents.runner import RunContext, run_role
    from finresearch.bridge import Tier, build_router
    from finresearch.config import get_settings
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun

    s = get_settings()
    with session_scope() as db:
        co = db.query(Company).filter_by(slug=company_slug).one_or_none()
        if co is None:
            raise ValueError(f"unknown company {company_slug!r}")
        name, symbol, co_id = co.name, co.nse_symbol, co.id
    cands: list[Candidate] = []
    if symbol:
        try:
            from finresearch.adapters.nse import NseClient

            async with NseClient() as nse:
                detail = await nse.ipo_detail(symbol)
            cands += nse_candidates(detail.issue_info)
            log(f"NSE: {len(cands)} archive link(s)")
        except Exception as e:
            log(f"NSE issue info unavailable: {e}")
    sebi = await sebi_candidates(name)
    log(f"SEBI: {len(sebi)} matching filing(s)")
    cands += sebi
    if use_agent:
        from finresearch.mcp_server.server import list_documents

        with session_scope() as db:
            run = ResearchRun(company_id=co_id, kind="discovery", status="running", manifest={})
            db.add(run)
            db.flush()
            run_id = run.id
        ctx = RunContext(run_id=run_id, company_slug=company_slug, company_name=name, nse_symbol=symbol,
                         documents=json.loads(list_documents(company_slug)))  # fmt: skip
        try:
            parsed, _ = await run_role("discovery", ctx)
            agent = agent_candidates(parsed.model_dump())
            log(f"discovery agent: {len(agent)} document link(s) on {len(parsed.ir_pages)} page(s)")
            cands += agent
            status = "done"
        except Exception as e:
            log(f"discovery agent failed: {e}")
            status = "failed"
        with session_scope() as db:
            db.get(ResearchRun, run_id).status = status
    embedder = build_router().engines.get(Tier.LOCAL) if index else None

    def _ingest() -> DiscoveryReport:
        with session_scope() as db:
            co = db.get(Company, co_id)
            return fetch_and_ingest(db, co, cands, docs_dir=s.docs_dir, embedder=embedder, index=index)

    # ingestion and indexing are synchronous and call asyncio.run() for embeddings and OCR, so they must run in a
    # worker thread, never inside this coroutine's event loop
    return await asyncio.to_thread(_ingest)

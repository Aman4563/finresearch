"""Discover and ingest a company's offer and investor-relations documents.

Sources, most deterministic first:
1. NSE issue information: links to the RHP and the anchor-allocation report (ZIP archives of PDFs); for a BSE SME
   issue, BSE's issue details (prospectus ZIP, price-band ad) and offer-document list.
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
SAME_TEXT_PAGES = (
    0.9  # share of identical pages that makes two files the same document (e.g. re-signed copies)
)
NSE_ZIP_KINDS = {
    "Red Herring Prospectus": DocKind.RHP,
    "Anchor Allocation Report": DocKind.ANCHOR,
    "Ratios / Basis of Issue Price": DocKind.OTHER,
}  # the last one is published for SME issues
_STOP = {"limited", "ltd", "private", "pvt", "india", "the", "and", "&", "co", "company", "corporation"}


@dataclass
class Candidate:
    url: str
    kind: DocKind
    title: str
    source: str  # nse | bse | sebi | agent
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
    if re.search(r"(?:^|[^a-z])gid(?:[^a-z]|$)|general[ _-]*information", t):
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


BSE_DOC_KINDS = {"Red Herring Prospectus": DocKind.RHP, "Price Band Advertisement": DocKind.PRICE_BAND_AD,
                 "Addendum": DocKind.ADDENDUM, "Corrigendum": DocKind.ADDENDUM,
                 "Anchor Allocation Report": DocKind.ANCHOR}  # fmt: skip


def bse_candidates(company_name: str, detail=None, offer_docs=()) -> list[Candidate]:
    """A BSE SME issue's document links (RHP/prospectus ZIP, price-band ad) from its issue details, plus any matching
    row of BSE's offer-document list (DRHP / RHP / prospectus PDFs)."""
    ref = "https://www.bseindia.com/"
    out = []
    for label, url in detail.documents.items() if detail is not None else ():
        if label in BSE_DOC_KINDS:
            out.append(Candidate(url, BSE_DOC_KINDS[label], f"BSE {label}", "bse", ref))
    for d in offer_docs:
        if names_match(company_name, d.company):
            for url, kind, title in ((d.prospectus, DocKind.RHP, "Prospectus"), (d.rhp, DocKind.RHP, "RHP"),
                                     (d.drhp, DocKind.DRHP, "DRHP")):  # fmt: skip
                if url:
                    out.append(Candidate(url, kind, f"BSE {title}", "bse", ref))
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


async def stock_candidates(
    symbol: str, *, report_years: int = 2, results: int = 4, equity=None
) -> list[Candidate]:
    """A listed stock's recent annual reports and results filings, from NSE."""
    from finresearch.adapters.nse_equity import NseEquity, latest_annual_reports

    out: list[Candidate] = []
    page = f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}"
    async with equity or NseEquity() as eq:
        for f in latest_annual_reports(await eq.annual_reports(symbol), report_years):
            out.append(
                Candidate(f.url, DocKind.ANNUAL_REPORT, f"Annual report {f.fiscal_label}", "nse", page)
            )
        filings = sorted((a for a in await eq.announcements(symbol) if a.results_period_end and a.attachment),
                         key=lambda a: a.results_period_end, reverse=True)  # fmt: skip
        seen = set()
        for a in filings:
            if a.results_period_end in seen:
                continue
            seen.add(a.results_period_end)
            out.append(Candidate(a.attachment, DocKind.FINANCIALS,
                                 f"Financial results for the period ended {a.results_period_end}", "nse", page))  # fmt: skip
            if len(seen) >= results:
                break
    return out


# --------------------------------------------------------------------------- download & ingest
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def same_text_as(session: Session, doc) -> int | None:
    """Id of another document of the same company with (almost) the same page texts, if any.

    Exchanges and SEBI host byte-different copies of the same offer document (different signature pages or
    metadata), so sha256 alone does not catch them.
    """
    from sqlalchemy import select

    from finresearch.db.models import Document, DocumentPage

    others = session.scalars(select(Document.id).where(Document.company_id == doc.company_id, Document.id != doc.id,
                                                       Document.pages == doc.pages)).all()  # fmt: skip
    if not others or not doc.pages:
        return None

    def pages(doc_id: int) -> dict[int, str]:
        rows = session.execute(
            select(DocumentPage.page_no, DocumentPage.text).where(DocumentPage.document_id == doc_id)
        )
        return {n: _norm(t) for n, t in rows}

    mine = pages(doc.id)
    for other in others:
        theirs = pages(other)
        same = sum(1 for n, t in mine.items() if t and theirs.get(n) == t)
        if same / doc.pages >= SAME_TEXT_PAGES:
            return other
    return None


def _drop_files(local_path: str, text_path: str | None) -> None:
    import shutil

    Path(local_path).unlink(missing_ok=True)
    if text_path:
        shutil.rmtree(Path(text_path).parent, ignore_errors=True)


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
                    if (twin := same_text_as(session, doc)) is not None:
                        paths = (doc.local_path, doc.text_path)
                        session.rollback()
                        _drop_files(*paths)
                        report.outcomes.append(Outcome(c, "duplicate", twin, f"same text as document {twin}"))
                        continue
                    if index:
                        index_document(session, doc, embedder=embedder)
                    session.commit()  # keep each finished document even if a later download or OCR fails
                except Exception as e:
                    session.rollback()
                    report.outcomes.append(Outcome(c, "failed", detail=f"ingest {name}: {e}"[:300]))
                    continue
                stored_as = f" as {doc.kind}" if doc.kind != c.kind.value else ""
                report.outcomes.append(Outcome(c, "ingested", doc.id, f"{doc.pages} pages{stored_as}"))
    return report


# --------------------------------------------------------------------------- orchestration
async def discover(
    company_slug: str, *, use_agent: bool = True, index: bool = True, log=print, kind: str = "ipo"
) -> DiscoveryReport:
    """Find, download and ingest a company's documents: for an IPO from NSE issue archives, SEBI and (optionally) its
    IR pages; for a listed stock (kind="stock") its NSE annual reports and results filings."""
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
        bse_ipo_no = (co.meta or {}).get("bse_ipo_no")
    cands: list[Candidate] = []
    if kind == "stock":
        if not symbol:
            raise ValueError(f"{company_slug} has no NSE symbol")
        cands = await stock_candidates(symbol)
        log(f"NSE: {len(cands)} annual report / results filing(s)")
        use_agent = False
    elif symbol:
        try:
            from finresearch.adapters.nse import NseClient

            async with NseClient() as nse:
                detail = await nse.ipo_detail(symbol)
            cands += nse_candidates(detail.issue_info)
            log(f"NSE: {len(cands)} archive link(s)")
        except Exception as e:
            log(f"NSE issue info unavailable: {e}")
    elif bse_ipo_no:
        try:
            from finresearch.adapters.bse import BseClient

            async with BseClient() as bse:
                detail = await bse.issue_detail(bse_ipo_no)
                cands += bse_candidates(name, detail, await bse.offer_documents())
            log(f"BSE: {len(cands)} document link(s)")
        except Exception as e:
            log(f"BSE issue details unavailable: {e}")
    if kind != "stock":
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

"""FinResearch MCP server: the tool surface Claude Code research agents use.

Run (stdio):  uv run --directory <repo> python -m finresearch.mcp_server
Claude Code config (passed by the bridge with --mcp-config / --strict-mcp-config):
  {"mcpServers": {"finresearch": {"command": "uv", "args": ["run", "--directory", "<repo>", "python",
   "-m", "finresearch.mcp_server"]}}}

Design notes
* Every tool that returns document text returns LINE NUMBERS and PAGE numbers so agents can cite precisely.
* Output is bounded (max lines / matches) and paginated, so no agent silently receives a truncated document.
* `save_claim` runs deterministic citation checks; `fincalc` exposes the tested arithmetic library so agents
  never do maths in their heads.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import func, select

from finresearch import fincalc
from finresearch.db import session_scope
from finresearch.db.models import Chunk, Company, Document, DocumentPage, ResearchRun, Section
from finresearch.ingest.layout_table import parse_layout_table
from finresearch.ingest.text import read_lines
from finresearch.mcp_server import claims as claims_mod

MAX_LINES = 400
MAX_MATCHES = 60

server = MCPServer(
    name="finresearch",
    instructions=(
        "Tools over FinResearch's document store (offer documents, annual reports) and claim ledger. "
        "Always cite document_id + line numbers from these tools. Record every factual or numeric finding with "
        "save_claim, quoting the exact text at the cited lines. Use fincalc for ALL arithmetic."
    ),
)


# --------------------------------------------------------------------------- helpers
def _jsonable(x: Any) -> Any:
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, datetime | date):
        return x.isoformat()
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(x).items()}
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple | set):
        return [_jsonable(v) for v in x]
    if hasattr(x, "model_dump"):
        return _jsonable(x.model_dump())
    return x


def _numbered(lines: list[str], start: int, end: int, page_spans: list[tuple[int, int, int]]) -> str:
    out, pi = [], 0
    for n in range(start, end + 1):
        while pi < len(page_spans) and page_spans[pi][2] < n:
            pi += 1
        if pi < len(page_spans) and page_spans[pi][1] == n:
            out.append(f"----- [page {page_spans[pi][0]}] -----")
        out.append(f"{n:>6}| {lines[n - 1].replace(chr(12), '')}")
    return "\n".join(out)


def _page_spans(session, doc_id: int) -> list[tuple[int, int, int]]:
    return [
        tuple(r)
        for r in session.execute(
            select(DocumentPage.page_no, DocumentPage.line_start, DocumentPage.line_end)
            .where(DocumentPage.document_id == doc_id)
            .order_by(DocumentPage.page_no)
        ).all()
    ]


def _doc(session, document_id: int) -> Document:
    doc = session.get(Document, document_id)
    if doc is None:
        raise ValueError(f"unknown document_id {document_id}; call list_documents first")
    return doc


# --------------------------------------------------------------------------- documents
@server.tool()
def list_documents(company: str | None = None) -> str:
    """List ingested documents (id, company, kind, title, pages, scanned pages, section count).
    `company` filters by slug, NSE symbol or name substring."""
    with session_scope() as s:
        q = select(Document, Company).outerjoin(Company, Company.id == Document.company_id)
        if company:
            like = f"%{company.lower()}%"
            q = q.where(
                func.lower(Company.slug).like(like)
                | func.lower(Company.name).like(like)
                | func.lower(func.coalesce(Company.nse_symbol, "")).like(like)
            )
        rows = []
        for d, c in s.execute(q.order_by(Document.id)).all():
            n_sec = s.scalar(select(func.count()).select_from(Section).where(Section.document_id == d.id))
            rows.append(
                {
                    "document_id": d.id,
                    "company": c.slug if c else None,
                    "kind": d.kind,
                    "title": d.title,
                    "pages": d.pages,
                    "scanned_pages": d.scanned_pages,
                    "sections": n_sec,
                    "source_url": d.source_url,
                }
            )
        return json.dumps(rows, indent=1)


@server.tool()
def list_sections(document_id: int) -> str:
    """Sections of an offer document (SEBI ICDR layout) with line and page ranges."""
    with session_scope() as s:
        _doc(s, document_id)
        secs = s.scalars(
            select(Section).where(Section.document_id == document_id).order_by(Section.line_start)
        )
        return json.dumps(
            [
                {
                    "section": x.canonical,
                    "heading": x.heading,
                    "lines": [x.line_start, x.line_end],
                    "pages": [x.page_start, x.page_end],
                    "n_lines": x.line_end - x.line_start + 1,
                }
                for x in secs
            ],
            indent=1,
        )


@server.tool()
def read_lines_tool(document_id: int, line_start: int, line_end: int) -> str:
    """Read document lines [line_start, line_end] (1-based, inclusive, max 400 per call) with line numbers
    and page markers. Use this to read sections in pages of <=400 lines."""
    with session_scope() as s:
        doc = _doc(s, document_id)
        lines = read_lines(doc.text_path)
        a, b = max(1, line_start), min(len(lines), line_end)
        if b < a:
            return f"empty range (document has {len(lines)} lines)"
        truncated = b - a + 1 > MAX_LINES
        b = min(b, a + MAX_LINES - 1)
        body = _numbered(lines, a, b, _page_spans(s, doc.id))
        tail = f"\n[... more lines: continue with line_start={b + 1}]" if truncated else ""
        return f"document {doc.id} ({doc.title}) lines {a}-{b} of {len(lines)}\n{body}{tail}"


@server.tool()
def read_section(document_id: int, section: str, offset: int = 0) -> str:
    """Read a mapped section (e.g. RISK_FACTORS, OBJECTS_OF_THE_OFFER, RESTATED_FINANCIALS, MDNA, LITIGATION)
    in pages of 400 lines; `offset` = lines to skip from the section start."""
    with session_scope() as s:
        sec = s.scalar(
            select(Section).where(Section.document_id == document_id, Section.canonical == section.upper())
        )
        if sec is None:
            names = s.scalars(select(Section.canonical).where(Section.document_id == document_id)).all()
            return f"section {section!r} not found. Available: {names}"
    start = sec.line_start + max(0, offset)
    if start > sec.line_end:
        return f"offset beyond section end (section has {sec.line_end - sec.line_start + 1} lines)"
    return read_lines_tool(document_id, start, min(sec.line_end, start + MAX_LINES - 1)) + (
        f"\n[section continues: call read_section(offset={offset + MAX_LINES})]"
        if start + MAX_LINES - 1 < sec.line_end
        else "\n[end of section]"
    )


@server.tool()
def grep_document(document_id: int, pattern: str, context: int = 0, ignore_case: bool = True,
                  max_matches: int = 40) -> str:  # fmt: skip
    """Regex search within one document. Returns matching lines (with `context` lines around) and pages."""
    with session_scope() as s:
        doc = _doc(s, document_id)
        spans = _page_spans(s, doc.id)
    lines = read_lines(doc.text_path)
    try:
        rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as e:
        return f"invalid regex: {e}"
    limit = min(max_matches, MAX_MATCHES)
    hits = [i for i, ln in enumerate(lines, 1) if rx.search(ln)]
    out = [
        f"{len(hits)} matching lines in document {doc.id}"
        + (f" (showing first {limit})" if len(hits) > limit else "")
    ]
    from finresearch.ingest.sections import page_for_line

    for n in hits[:limit]:
        a, b = max(1, n - context), min(len(lines), n + context)
        out.append(f"--- p{page_for_line(n, spans)} L{n}")
        out += [f"{k:>6}| {lines[k - 1].replace(chr(12), '')}" for k in range(a, b + 1)]
    return "\n".join(out)


@server.tool()
def search_documents(query: str, company: str | None = None, document_ids: list[int] | None = None,
                     k: int = 8) -> str:  # fmt: skip
    """Hybrid (keyword + semantic) search over document chunks. Returns section, page and line anchors;
    read around the anchors with read_lines_tool before citing."""
    from finresearch.bridge import Tier, build_router
    from finresearch.ingest.index import hybrid_search

    embedder = build_router().engines.get(Tier.LOCAL)
    with session_scope() as s:
        ids = document_ids
        if company and not ids:
            like = f"%{company.lower()}%"
            ids = s.scalars(
                select(Document.id)
                .join(Company)
                .where(func.lower(Company.slug).like(like) | func.lower(Company.name).like(like))
            ).all()
        has_vec = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.embedding.is_not(None))) > 0
        try:
            hits = hybrid_search(
                s, query, document_ids=ids or None, embedder=embedder if has_vec else None, k=k
            )
        except Exception:
            hits = hybrid_search(s, query, document_ids=ids or None, embedder=None, k=k)
    return json.dumps(
        [
            {
                "document_id": h.document_id,
                "document": h.document_title,
                "section": h.section,
                "pages": [h.page_start, h.page_end],
                "lines": [h.line_start, h.line_end],
                "score": h.score,
                "text": h.text[:1200],
            }
            for h in hits
        ],
        indent=1,
    )


@server.tool()
def extract_table(document_id: int, line_start: int, line_end: int) -> str:
    """Rebuild a financial table from the document's layout text (multi-line headers, shifted rows, notes
    column) into markdown with detected periods and units. Cite the same line range you pass in."""
    with session_scope() as s:
        doc = _doc(s, document_id)
    lines = read_lines(doc.text_path)
    t = parse_layout_table("\n".join(lines[line_start - 1 : line_end]))
    if t is None:
        return "no numeric table detected in that range"
    return json.dumps(
        {
            "lines": [line_start, line_end],
            "periods": t.periods,
            "unit": t.unit_note,
            "markdown": t.to_markdown(),
        },
        indent=1,
    )


# --------------------------------------------------------------------------- arithmetic
_FINCALC_MODULES = {
    m.__name__.rsplit(".", 1)[-1]: m
    for m in (fincalc.numbers, fincalc.growth, fincalc.ratios, fincalc.valuation, fincalc.ipo, fincalc.dates)
}


def _fincalc_catalog() -> dict[str, str]:
    cat = {}
    for mod_name, mod in _FINCALC_MODULES.items():
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("_") or fn.__module__ != mod.__name__:
                continue
            doc = (inspect.getdoc(fn) or "").split("\n")[0]
            cat[f"{mod_name}.{name}"] = f"{name}{inspect.signature(fn)} — {doc}"
    return cat


@server.tool()
def fincalc_functions() -> str:
    """List the deterministic finance functions available to `fincalc` (module.function -> signature, doc)."""
    return json.dumps(_fincalc_catalog(), indent=1)


@server.tool()
def fincalc_call(function: str, args: dict[str, Any]) -> str:
    """Call a finance function, e.g. function="growth.cagr", args={"start": "6577.67", "end": "11716.54",
    "years": 2}. Pass numbers as strings to keep precision; dates as "YYYY-MM-DD". Returns JSON result."""
    if function not in _fincalc_catalog():
        return f"unknown function {function!r}; call fincalc_functions()"
    mod_name, name = function.split(".", 1)
    fn = getattr(_FINCALC_MODULES[mod_name], name)
    params = inspect.signature(fn).parameters
    kwargs = {}
    for k, v in (args or {}).items():
        if k not in params:
            return f"unexpected argument {k!r}; signature is {name}{inspect.signature(fn)}"
        if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            v = date.fromisoformat(v)
        elif (
            isinstance(v, list)
            and v
            and all(isinstance(x, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x) for x in v)
        ):
            v = [date.fromisoformat(x) for x in v]
        kwargs[k] = v
    try:
        return json.dumps({"function": function, "result": _jsonable(fn(**kwargs))})
    except (ValueError, TypeError, ArithmeticError) as e:
        return json.dumps({"function": function, "error": f"{type(e).__name__}: {e}"})


# --------------------------------------------------------------------------- market data
@server.tool()
async def nse_ipo_detail(symbol: str) -> str:
    """LIVE NSE IPO data for a symbol: combined NSE+BSE subscription by category (activeCat, with NSE's
    timestamp), NSE-only bids, price-wise demand and issue info (dates, price band, lot, UPI cut-off).
    Figures are INTERIM while bidding is open; NSE computes times on the LOWER price-band share base.
    Each call is stored as a timestamped snapshot."""
    from finresearch.adapters.nse import NseClient
    from finresearch.db.models import SubscriptionSnapshotRow

    async with NseClient() as nse:
        d = await nse.ipo_detail(symbol.upper())
    snap = d.combined
    if snap is not None and snap.as_of is not None:
        with session_scope() as s:
            exists = s.scalar(
                select(SubscriptionSnapshotRow.id).where(
                    SubscriptionSnapshotRow.nse_symbol == snap.symbol,
                    SubscriptionSnapshotRow.as_of == snap.as_of,
                    SubscriptionSnapshotRow.source == snap.source,
                )
            )
            if exists is None:
                s.add(
                    SubscriptionSnapshotRow(
                        nse_symbol=snap.symbol,
                        as_of=snap.as_of,
                        source=snap.source,
                        total_times=snap.total_times,
                        categories=_jsonable([c.model_dump() for c in snap.categories]),
                        raw={"issue_info": d.issue_info},
                    )
                )
    return json.dumps(
        _jsonable(
            {
                "symbol": d.symbol,
                "company": d.company_name,
                "issue_info": d.issue_info,
                "combined_nse_bse": d.combined.model_dump() if d.combined else None,
                "nse_only": d.nse_only.model_dump() if d.nse_only else None,
                "demand_combined": d.demand_combined.model_dump() if d.demand_combined else None,
                "fetched_at": d.fetch.fetched_at if d.fetch else None,
                "note": "INTERIM while bidding is open; times on lower price-band share base",
            }
        ),
        indent=1,
    )


@server.tool()
async def nse_current_issues() -> str:
    """LIVE list of IPOs currently open for bidding on NSE (symbol, company, dates, price band, times)."""
    from finresearch.adapters.nse import NseClient

    async with NseClient() as nse:
        rows = await nse.current_issues()
    return json.dumps(_jsonable([r.model_dump() if hasattr(r, "model_dump") else r for r in rows]), indent=1)


@server.tool()
async def sebi_filings(kind: str = "rhp", pages: int = 1) -> str:
    """Recent SEBI public-issue filings (kind: drhp | rhp | prospectus | other | all) with detail URLs.
    Use sebi_resolve_pdf(detail_url) to get the full-document PDF link."""
    from finresearch.adapters.sebi import SebiClient

    async with SebiClient() as sebi:
        rows = await sebi.list_public_issue_filings(kind=kind, pages=min(pages, 5))
    return json.dumps(_jsonable([r.model_dump() for r in rows]), indent=1)


@server.tool()
async def sebi_resolve_pdf(detail_url: str) -> str:
    """PDF URL(s) for a SEBI filing detail page (full document first)."""
    from finresearch.adapters.sebi import SebiClient

    async with SebiClient() as sebi:
        return json.dumps(await sebi.resolve_pdf_url(detail_url))


# --------------------------------------------------------------------------- claim ledger
@server.tool()
def start_run(company: str, kind: str = "ipo_report", note: str | None = None) -> str:
    """Create a research run (container for claims). Returns run_id."""
    with session_scope() as s:
        c = s.scalar(select(Company).where(Company.slug == company))
        run = ResearchRun(company_id=c.id if c else None, kind=kind, manifest={"note": note} if note else {})
        s.add(run)
        s.flush()
        return json.dumps({"run_id": run.id, "company_found": c is not None})


@server.tool()
def save_claim(run_id: int, stream: str, statement: str, claim_type: str, citations: list[dict[str, Any]],
               metric: str | None = None, value: str | None = None, unit: str | None = None,
               period: str | None = None, importance: str = "normal") -> str:  # fmt: skip
    """Record one finding in the claim ledger.
    claim_type: numeric | factual | opinion. importance: high | normal | low.
    citations: [{"document_id": 1, "line_start": 1650, "line_end": 1665, "quote": "<exact text at those lines>"}]
               or [{"url": "https://...", "accessed_at": "2026-09-28T14:00:00+05:30", "quote": "..."}].
    The server checks each document quote really appears at the cited lines; a claim whose quotes are all
    missing is stored as 'unsupported' — fix the citation and save again."""
    with session_scope() as s:
        try:
            res = claims_mod.save_claim(
                s,
                run_id=run_id,
                stream=stream,
                statement=statement,
                claim_type=claim_type,
                citations=citations,
                metric=metric,
                value=value,
                unit=unit,
                period=period,
                importance=importance,
            )
        except ValueError as e:
            return json.dumps({"error": str(e)})
        return json.dumps(res)


@server.tool()
def list_claims(run_id: int, stream: str | None = None, status: str | None = None) -> str:
    """Claims recorded for a run (optionally filtered by stream or status) with their citation checks."""
    from finresearch.db.models import Claim

    with session_scope() as s:
        q = select(Claim).where(Claim.run_id == run_id)
        if stream:
            q = q.where(Claim.stream == stream)
        if status:
            q = q.where(Claim.status == status)
        out = []
        for c in s.scalars(q.order_by(Claim.id)):
            out.append(
                {
                    "claim_id": c.id,
                    "stream": c.stream,
                    "statement": c.statement,
                    "type": c.claim_type,
                    "metric": c.metric,
                    "value": _jsonable(c.value),
                    "unit": c.unit,
                    "period": c.period,
                    "status": c.status,
                    "importance": c.importance,
                    "citations": [
                        {
                            "document_id": x.document_id,
                            "page": x.page_no,
                            "lines": [x.line_start, x.line_end],
                            "quote_found": x.quote_found,
                            "url": x.url,
                        }
                        for x in c.citations
                    ],
                }
            )
        return json.dumps(out, indent=1)


def main() -> None:
    server.run("stdio")

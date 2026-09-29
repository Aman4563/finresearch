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
from datetime import UTC, date, datetime
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
    for m in (
        fincalc.numbers,
        fincalc.growth,
        fincalc.ratios,
        fincalc.valuation,
        fincalc.ipo,
        fincalc.dates,
        fincalc.market,
    )
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


# --------------------------------------------------------------------------- listed bonds
@server.tool()
async def nse_bond_search(query: str) -> str:
    """Find listed bonds / NCDs traded on NSE by symbol fragment (e.g. "NHAI", "SCL") or ISIN: coupon, face value,
    last price, maturity, rating and warnings (partly redeemed face value, stale interest dates). Cite
    https://www.nseindia.com/market-data/bonds-traded-in-capital-market with the access time."""
    from finresearch.adapters.nse_bonds import live_bonds, search_bonds

    hits = search_bonds(await live_bonds(), query)
    return json.dumps([{**b.model_dump(mode="json"), "warnings": b.warnings} for b in hits], indent=1)


@server.tool()
async def bond_analytics(isin: str, coupon_frequency: int, price_basis: str = "dirty", settlement: str | None = None,
                         tax_rate: str | None = None) -> str:  # fmt: skip
    """Yield to maturity, current yield, accrued interest, Macaulay/modified duration and convexity for a listed bond
    from NSE's last price, coupon and maturity (fincalc.bonds). `coupon_frequency` (1, 2, 4 or 12) must come from
    the offer document or information memorandum, never assumed. With `tax_rate` it returns the after-tax YTM from
    after-tax cash flows. `price_basis` defaults to "dirty": NSE states that bonds traded
    in the capital-market segment "are traded & settled on Dirty Price i.e. including accrued interest"
    (https://www.nseindia.com/market-data/bonds-traded-in-capital-market); pass "clean" for clean quotes. Pass `tax_rate` (a fraction) for
    the post-tax yield. Refuses partly redeemed bonds, whose cash flows need the redemption schedule."""
    from datetime import date as _date

    from finresearch.adapters.nse_bonds import live_bonds
    from finresearch.fincalc import bonds as b
    from finresearch.fincalc.dates import today_ist

    bond = next((x for x in await live_bonds() if x.isin.upper() == isin.upper()), None)
    if bond is None:
        return json.dumps({"error": f"{isin} is not in NSE's list of traded bonds"})
    if any("partly" in w for w in bond.warnings):
        return json.dumps({"error": bond.warnings[0], "bond": bond.model_dump(mode="json")})
    if not (bond.last_price and bond.coupon_pct is not None and bond.maturity and bond.face_value):
        return json.dumps(
            {"error": "price, coupon, maturity or face value missing", "bond": bond.model_dump(mode="json")}
        )
    s = _date.fromisoformat(settlement) if settlement else today_ist()
    coupon = bond.coupon_pct / 100
    ai = b.accrued_interest(s, bond.maturity, coupon, coupon_frequency, bond.face_value)
    if price_basis not in ("dirty", "clean"):
        return json.dumps({"error": "price_basis must be 'dirty' or 'clean'"})
    clean = bond.last_price - ai if price_basis == "dirty" else bond.last_price
    y = b.ytm(clean, s, bond.maturity, coupon, coupon_frequency, bond.face_value)
    d = b.duration(y, s, bond.maturity, coupon, coupon_frequency, bond.face_value)
    out = {"bond": bond.model_dump(mode="json"), "settlement": s.isoformat(), "coupon_frequency": coupon_frequency,
           "accrued_interest": str(ai.quantize(Decimal("0.0001"))), "clean_price": str(clean.quantize(Decimal("0.0001"))),
           "ytm": str(y), "current_yield": str(b.current_yield(bond.last_price, coupon, bond.face_value)),
           "macaulay_duration_years": str(d.macaulay), "modified_duration": str(d.modified), "convexity": str(d.convexity),
           "price_basis": price_basis,
           "price_basis_source": "NSE: CM-segment bonds are traded and settled on dirty price (bonds-traded-in-capital-market page)"
           if price_basis == "dirty" else "stated by the caller",
           "conventions": "accrued interest Actual/Actual (SEBI CIR/IMD/DF-1/122/2016); discounting by coupon periods",
           "warnings": bond.warnings}  # fmt: skip
    if tax_rate is not None:
        # live bond run 12: YTM x (1 - t) overstated the post-tax yield of a premium bond (pull-to-par is a
        # capital loss, not interest); compute it from after-tax cash flows instead
        out["after_tax_ytm"] = str(b.after_tax_ytm(clean, s, bond.maturity, coupon, coupon_frequency, tax_rate,
                                                   face=bond.face_value))  # fmt: skip
        out["after_tax_note"] = (
            "coupons taxed at tax_rate; the premium over face is a capital loss at redemption "
            "that saves tax only if the investor can offset it"
        )
    return json.dumps(out, indent=1)


# --------------------------------------------------------------------------- mutual funds (AMFI)
_NAV_ALL: dict[str, Any] = {}


async def _nav_all(amfi) -> list:
    import time

    if _NAV_ALL.get("at", 0) < time.time() - 6 * 3600:
        _NAV_ALL.update(at=time.time(), rows=await amfi.nav_all())
    return _NAV_ALL["rows"]


def _scheme_json(x) -> dict[str, Any]:
    return {"scheme_code": x.code, "name": x.name, "plan": x.plan, "option": x.option, "category": x.category,
            "amc": x.amc, "nav": str(x.nav) if x.nav is not None else None,
            "nav_date": x.day.isoformat() if x.day else None, "isin": x.isin_growth}  # fmt: skip


@server.tool()
async def amfi_scheme_search(query: str) -> str:
    """Find mutual-fund schemes in AMFI's daily NAV file by scheme code or name words (direct-growth first). Returns
    code, plan, option, SEBI category, AMC and the latest NAV with its date. Cite https://www.amfiindia.com/spages/NAVAll.txt."""
    from finresearch.adapters.amfi import AmfiClient, search_schemes

    async with AmfiClient() as amfi:
        rows = await _nav_all(amfi)
    return json.dumps([_scheme_json(x) for x in search_schemes(rows, query)], indent=1)


@server.tool()
async def amfi_nav_history(scheme_code: str, years: int = 5, risk_free_annual: str | None = None) -> str:
    """AMFI NAV history of a scheme (up to `years` years) with deterministic statistics: trailing 1/3/5-year
    annualised returns, 3-year rolling-return range, annualised volatility and max drawdown (fincalc), and
    Sharpe/Sortino when you pass a risk-free rate (a fraction, e.g. "0.065", with its source cited separately).
    Cite AMFI's NAV history report URL with today's access time."""
    from datetime import timedelta

    from finresearch.adapters.amfi import AmfiClient
    from finresearch.config import get_settings
    from finresearch.fincalc import funds, market
    from finresearch.fincalc.dates import today_ist

    today = today_ist()
    async with AmfiClient(cache_dir=get_settings().state_dir) as amfi:
        scheme = next((x for x in await _nav_all(amfi) if x.code == scheme_code), None)
        if scheme is None:
            return json.dumps({"error": f"scheme {scheme_code} is not in AMFI's NAV file"})
        probe = today - timedelta(days=3 if today.weekday() == 0 else 1)
        # a few extra days so the N-year trailing return finds a NAV on or before its start date
        hist = await amfi.scheme_history(
            scheme, today.replace(year=today.year - years) - timedelta(days=10), today, probe
        )
    navs = [(h.day, h.nav) for h in hist]
    stats: dict[str, Any] = {"points": len(navs)}
    if len(navs) >= 3:
        closes = [v for _, v in navs]
        stats.update({f"trailing_{y}y": str(r) if (r := funds.trailing_return(navs, y)) is not None else None
                      for y in (1, 3, 5)})  # fmt: skip
        roll = funds.rolling_returns(navs, 3)
        stats["rolling_3y"] = {k: str(v) for k, v in roll.__dict__.items()} if roll else None
        stats["annualised_volatility"] = str(market.annualised_volatility(closes))
        stats["max_drawdown"] = str(market.max_drawdown(closes).max_drawdown)
        if risk_free_annual is not None:
            stats["risk_free_annual"] = risk_free_annual
            stats["sharpe"] = str(funds.sharpe_ratio(navs, risk_free_annual))
            stats["sortino"] = str(funds.sortino_ratio(navs, risk_free_annual))
    sample = navs[:: max(1, len(navs) // 60)] + navs[-1:]
    return json.dumps({"scheme": _scheme_json(scheme), "source": "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx",
                       "first": {"date": str(navs[0][0]), "nav": str(navs[0][1])} if navs else None,
                       "last": {"date": str(navs[-1][0]), "nav": str(navs[-1][1])} if navs else None,
                       "stats": stats, "sample": [[str(d), str(v)] for d, v in sample]}, indent=1)  # fmt: skip


@server.tool()
async def amfi_category_peers(scheme_code: str, limit: int = 25) -> str:
    """Direct-growth schemes in the same SEBI category with point-to-point 1/3/5-year annualised returns from AMFI
    NAVs (the scheme itself included), ranked by 3-year return. Cite AMFI's NAV history report."""
    from finresearch.adapters.amfi import AmfiClient
    from finresearch.fincalc import funds
    from finresearch.fincalc.dates import today_ist

    today = today_ist()
    async with AmfiClient() as amfi:
        rows = await _nav_all(amfi)
        me = next((x for x in rows if x.code == scheme_code), None)
        if me is None:
            return json.dumps({"error": f"scheme {scheme_code} is not in AMFI's NAV file"})
        peers = [x for x in rows if x.category == me.category and x.is_direct_growth and x.nav]
        # anchor on the latest NAV date (as trailing returns do), not today, so both tools agree
        anchor = me.day or today
        past = {y: await amfi.navs_on(anchor.replace(year=anchor.year - y)) for y in (1, 3, 5)}
    table = []
    for p in peers:
        row = {"scheme_code": p.code, "name": p.name, "amc": p.amc, "nav": str(p.nav), "nav_date": str(p.day)}
        for y, snap in past.items():
            old = snap.get(p.code)
            row[f"return_{y}y"] = (str(funds.annualised_return(old.nav, p.nav, old.day, p.day))
                                   if old and old.nav and p.day else None)  # fmt: skip
        table.append(row)
    table.sort(key=lambda r: Decimal(r["return_3y"]) if r["return_3y"] else Decimal(-99), reverse=True)
    return json.dumps({"category": me.category, "peers": len(table), "table": table[:limit]}, indent=1)


# --------------------------------------------------------------------------- listed-stock data
def _equity_json(rows) -> str:
    return json.dumps([r.model_dump(mode="json") for r in rows], indent=1)


@server.tool()
async def nse_price_history(symbol: str, start: str, end: str) -> str:
    """Daily NSE prices for a listed stock between two ISO dates (open/high/low/close, VWAP, volume, value and
    52-week high/low), oldest first, plus deterministic summary stats (return, annualised volatility, max
    drawdown). Cite the NSE quote page URL with today's access time."""
    from finresearch.adapters.nse_equity import NseEquity
    from finresearch.fincalc import market

    async with NseEquity() as eq:
        bars = await eq.history(symbol.upper(), date.fromisoformat(start), date.fromisoformat(end))
    closes = [b.close for b in bars if b.close]
    stats = {}
    if len(closes) >= 3:
        dd = market.max_drawdown(closes)
        stats = {"return": str(market.price_return(closes[0], closes[-1])),
                 "annualised_volatility": str(market.annualised_volatility(closes)),
                 "max_drawdown": str(dd.max_drawdown), "bars": len(closes)}  # fmt: skip
    return json.dumps({"symbol": symbol.upper(), "source": f"https://www.nseindia.com/get-quotes/equity?symbol={symbol.upper()}",
                       "stats": stats, "bars": [b.model_dump(mode="json") for b in bars]}, indent=1)  # fmt: skip


@server.tool()
async def nse_announcements(symbol: str, limit: int = 40) -> str:
    """Latest NSE corporate announcements for a listed stock (category, text, attachment PDF, time). Filings that
    contain financial results carry results_period_end; ingest their PDFs with the document tools."""
    from finresearch.adapters.nse_equity import NseEquity

    async with NseEquity() as eq:
        anns = await eq.announcements(symbol.upper())
    return _equity_json(
        sorted(anns, key=lambda a: a.at or datetime.min.replace(tzinfo=UTC), reverse=True)[:limit]
    )


@server.tool()
async def nse_results_facts(xbrl_url: str) -> str:
    """Key reported figures (revenue, expenses, PBT, tax, PAT, EPS, ...) from a results filing's XBRL, for the
    quarter and year to date, in rupees. Use the xbrl link from nse_results_filings; cite the XBRL URL."""
    from finresearch.adapters.nse_equity import NseEquity
    from finresearch.adapters.xbrl import parse_results_xbrl

    if not xbrl_url.startswith("https://nsearchives.nseindia.com/"):
        return json.dumps({"error": "only NSE archive XBRL links are accepted"})
    async with NseEquity() as eq:
        x = parse_results_xbrl(await eq.fetch_bytes(xbrl_url))
    return json.dumps({"symbol": x.symbol, "consolidated": x.consolidated, "audited": x.audited, "url": xbrl_url,
                       "periods": {k: {"start": str(p.start), "end": str(p.end), "unit": "INR (EPS: INR per share)",
                                       "facts": {f: str(v) for f, v in p.facts.items()}} for k, p in x.periods.items()}},
                      indent=1)  # fmt: skip


@server.tool()
async def nse_results_filings(symbol: str, period: str = "Quarterly") -> str:
    """NSE's index of results filings (period, consolidated/standalone, audited, XBRL link). NSE's index can lag;
    the announcements list is the fresher source of results PDFs."""
    from finresearch.adapters.nse_equity import NseEquity

    async with NseEquity() as eq:
        return _equity_json(await eq.results(symbol.upper(), period))


@server.tool()
async def nse_shareholding(symbol: str) -> str:
    """Quarterly shareholding pattern (promoter and promoter group %, public %, employee trusts %), newest first."""
    from finresearch.adapters.nse_equity import NseEquity

    async with NseEquity() as eq:
        return _equity_json(await eq.shareholding(symbol.upper()))


@server.tool()
async def nse_corporate_actions(symbol: str) -> str:
    """Corporate actions (dividends with the per-share amount parsed, bonus, split, buyback) with ex and record
    dates."""
    from finresearch.adapters.nse_equity import NseEquity

    async with NseEquity() as eq:
        return _equity_json(await eq.corporate_actions(symbol.upper()))


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

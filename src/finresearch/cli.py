"""`finresearch` command line."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from finresearch.bridge import AgentTask, AllTiersFailed, Capability, ModelClass, Tier, build_router
from finresearch.config import get_settings

app = typer.Typer(no_args_is_help=True, help="FinResearch — personal research engine")
bridge_app = typer.Typer(no_args_is_help=True, help="Claude Bridge: engines, limits, test runs")
app.add_typer(bridge_app, name="bridge")
docs_app = typer.Typer(no_args_is_help=True, help="Documents: ingest, sections, index, search")
app.add_typer(docs_app, name="docs")
mcp_app = typer.Typer(no_args_is_help=True, help="FinResearch MCP server")
app.add_typer(mcp_app, name="mcp")
ipo_app = typer.Typer(no_args_is_help=True, help="IPO research reports (multi-agent pipeline)")
app.add_typer(ipo_app, name="ipo")
eval_app = typer.Typer(no_args_is_help=True, help="Evaluate runs against gold sets")
app.add_typer(eval_app, name="eval")
audit_app = typer.Typer(no_args_is_help=True, help="Read-only data audits against the exchanges' own figures")
app.add_typer(audit_app, name="audit")
console = Console()
logging.getLogger("httpx").setLevel(logging.WARNING)


@bridge_app.command()
def health() -> None:
    """Check every engine tier (login, models, limits)."""
    router = build_router()
    report = asyncio.run(router.health())
    t = Table(title="Claude Bridge health")
    t.add_column("tier")
    t.add_column("usable")
    t.add_column("details")
    for tier, h in report.items():
        usable = "[green]yes[/]" if h.get("usable_now") else f"[red]no[/] {h.get('blocked_reason') or ''}"
        details = {k: v for k, v in h.items() if k not in ("tier", "usable_now", "blocked_reason")}
        t.add_row(tier, usable, json.dumps(details, default=str))
    s = get_settings()
    if not (s.claude_api_enabled and s.anthropic_api_key):
        t.add_row("claude_api", "[yellow]disabled[/]", "no ANTHROPIC_API_KEY configured (by choice)")
    console.print(t)


@bridge_app.command()
def limits() -> None:
    """Show the last known subscription-window utilisation and any cool-downs."""
    router = build_router()
    state = router.tracker.describe()
    for tier, st in state.items():
        snap = st.get("snapshot") or {}
        console.print(f"[bold]{tier}[/]")
        if snap:
            for win in ("five_hour", "seven_day"):
                u, r = snap.get(f"{win}_utilization"), snap.get(f"{win}_resets_at")
                if u is not None:
                    reset = time.strftime("%a %H:%M", time.localtime(r)) if r else "?"
                    console.print(f"  {win}: {u:.0%} used, resets {reset}")
            console.print(f"  status: {snap.get('status')}  overage: {snap.get('overage_status')}")
        if st.get("cooling_until", 0) > time.time():
            console.print(f"  [red]cooling until {time.strftime('%H:%M', time.localtime(st['cooling_until']))}[/]"
                          f" — {st.get('reason')}")  # fmt: skip
    if not state:
        console.print("no data yet — run a task first")


@bridge_app.command()
def reset(tier: Tier | None = typer.Option(None, help="Tier to reset (default: all)")) -> None:
    """Clear cool-downs / circuit breakers (e.g. after a limit window has reset)."""
    build_router().tracker.reset(tier)
    console.print("reset done")


@bridge_app.command()
def run(
    prompt: str = typer.Argument(..., help="Prompt text, or @path/to/file"),
    schema: Path | None = typer.Option(None, help="JSON schema file for structured output"),
    model_class: ModelClass = typer.Option(ModelClass.FAST, "--model-class"),
    tier: Tier | None = typer.Option(None, help="Force a single tier"),
    web: bool = typer.Option(False, help="Allow WebSearch/WebFetch (Claude tiers only)"),
    effort: str | None = typer.Option(None),
    max_turns: int = typer.Option(8),
) -> None:
    """Run one task through the bridge and print the result."""
    text = Path(prompt[1:]).read_text() if prompt.startswith("@") else prompt
    task = AgentTask(
        name="cli-run",
        prompt=text,
        json_schema=json.loads(schema.read_text()) if schema else None,
        model_class=model_class,
        effort=effort,
        max_turns=max_turns,
        capabilities={Capability.WEB} if web else set(),
        allowed_tools=["WebSearch", "WebFetch"] if web else [],
        run_dir=get_settings().runs_dir / "cli",
    )
    try:
        res = asyncio.run(build_router().run(task, force_tier=tier))
    except AllTiersFailed as e:
        console.print(f"[red]FAILED[/] {e}")
        raise typer.Exit(1) from e
    console.print(f"[bold]tier[/]={res.tier.value} model={res.model} degraded={res.degraded} "
                  f"{res.duration_s:.1f}s est=${res.cost_usd_estimate:.4f}")  # fmt: skip
    console.print(f"attempts: {res.attempts}")
    if res.warnings:
        console.print(f"[yellow]warnings:[/] {res.warnings}")
    out = res.structured_output if res.structured_output is not None else res.text
    console.print_json(json.dumps(out)) if not isinstance(out, str) else console.print(out)


# --------------------------------------------------------------------------- docs
@docs_app.command("add")
def docs_add(
    source: str = typer.Argument(..., help="Local PDF path or http(s) URL"),
    company: str = typer.Option(..., help="Company slug, e.g. orient-cables"),
    kind: str = typer.Option(
        "OTHER",
        help="RHP|DRHP|ADDENDUM|ABRIDGED_PROSPECTUS|PRICE_BAND_AD|ANCHOR_ALLOCATION|"
        "ANNUAL_REPORT|FINANCIAL_STATEMENTS|INDUSTRY_REPORT|OTHER",
    ),
    title: str | None = typer.Option(None),
    name: str | None = typer.Option(None, help="Company display name (first time)"),
    nse_symbol: str | None = typer.Option(None),
    index: bool = typer.Option(True, help="Build sections + chunks + embeddings after ingest"),
    glm_ocr: bool = typer.Option(
        False, help="Second-opinion OCR with glm-ocr for low-confidence pages (slow)"
    ),
) -> None:
    """Ingest a document (download if URL), OCR scanned pages, map sections, index for search."""
    from finresearch.db import session_scope
    from finresearch.ingest.documents import DocKind, download, get_or_create_company, ingest_pdf
    from finresearch.ingest.index import index_document

    s = get_settings()
    router = build_router()
    local = router.engines.get(Tier.LOCAL)
    prov = None
    path = Path(source)
    if source.startswith(("http://", "https://")):
        path, prov = download(source, s.docs_dir / "incoming")
    with session_scope() as db:
        co = get_or_create_company(db, company, name, nse_symbol=nse_symbol)
        doc = ingest_pdf(
            db,
            path,
            company=co,
            kind=DocKind(kind.upper()),
            title=title,
            docs_dir=s.docs_dir,
            provenance=prov,
            glm_ocr=local if glm_ocr else None,
            progress=console.print,
        )
        console.print(f"document {doc.id}: {doc.pages} pages ({doc.scanned_pages} OCR'd) -> {doc.text_path}")
        if index:
            n = index_document(db, doc, embedder=local, progress=lambda m: console.print(m, end="\r"))
            console.print(f"\nindexed {n} chunks")
    if prov:
        path.unlink(missing_ok=True)


@docs_app.command("discover")
def docs_discover(
    company: str = typer.Argument(..., help="Company slug"),
    name: str | None = typer.Option(None, help="Company name (creates the company if new)"),
    nse_symbol: str | None = typer.Option(None),
    bse_ipo: int | None = typer.Option(
        None, help="BSE IPO number of a BSE-only SME issue (`finresearch ipo bse`)"
    ),
    bse_code: str | None = typer.Option(
        None, help="BSE scrip code of a BSE-only listed stock (with --kind stock)"
    ),
    agent: bool = typer.Option(True, help="Also search company IR pages with the discovery agent"),
    index: bool = typer.Option(True, help="Build sections, chunks and embeddings"),
    kind: str = typer.Option(
        "ipo", help="ipo (offer documents) or stock (annual reports and results filings)"
    ),
) -> None:
    """Find, download and ingest a company's offer and IR documents (NSE, SEBI, IR pages)."""
    from finresearch.db import session_scope
    from finresearch.ingest.discover import discover
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as db:
        co = get_or_create_company(db, company, name, nse_symbol=nse_symbol)
        if nse_symbol and not co.nse_symbol:
            co.nse_symbol = nse_symbol
        if bse_code and not co.bse_code:
            co.bse_code = bse_code
        _set_bse_ipo(co, bse_ipo)
    rep = asyncio.run(discover(company, use_agent=agent, index=index, log=console.print, kind=kind))
    for o in rep.outcomes:
        console.print(
            f"  {o.status:<9} {o.candidate.kind.value:<22} {o.candidate.title[:60]} "
            f"({o.candidate.source}) {o.detail or ''}"
        )
    console.print(rep.summary())


@docs_app.command("list")
def docs_list(company: str | None = typer.Option(None)) -> None:
    """List ingested documents."""
    from finresearch.mcp_server.server import list_documents

    console.print_json(list_documents(company))


@docs_app.command("sections")
def docs_sections(document_id: int) -> None:
    """Show mapped sections of a document."""
    from finresearch.mcp_server.server import list_sections

    console.print_json(list_sections(document_id))


@docs_app.command("search")
def docs_search(
    query: str, company: str | None = typer.Option(None), k: int = typer.Option(5, "-k", "--k")
) -> None:
    """Hybrid keyword + semantic search with page/line anchors."""
    from finresearch.mcp_server.server import search_documents

    for h in json.loads(search_documents(query, company=company, k=k)):
        console.print(
            f"[bold]doc {h['document_id']}[/] {h['section']} p{h['pages'][0]} L{h['lines'][0]}-"
            f"{h['lines'][1]}  score={h['score']}"
        )
        console.print("   " + h["text"].strip()[:300].replace("\n", " "))


@mcp_app.command("config")
def mcp_config() -> None:
    """Write the Claude Code --mcp-config file for the FinResearch MCP server and print its path."""
    from finresearch.mcp_server.config import write_mcp_config

    console.print(str(write_mcp_config()))


# --------------------------------------------------------------------------- ipo
def _pipeline(run_id: int, streams: str | None, concurrency: int | None):
    from finresearch.orchestrator.ipo import PipelineConfig
    from finresearch.orchestrator.kinds import KINDS, kind_of, pipeline_for

    allowed = KINDS[kind_of(run_id)].default_streams
    # None: the pipeline keeps the streams/concurrency saved when the run started (or the kind's defaults)
    chosen = tuple(x.strip() for x in streams.split(",")) if streams else None
    unknown = set(chosen or ()) - set(allowed)
    if unknown:
        raise typer.BadParameter(f"unknown streams {sorted(unknown)}; choose from {allowed}")
    cfg = PipelineConfig(
        streams=chosen, concurrency=concurrency, five_hour_ceiling=get_settings().max_five_hour_ceiling
    )
    return pipeline_for(run_id, config=cfg)


def _go(run_id: int, streams: str | None, concurrency: int | None, wait: bool) -> None:
    """Run (or resume) a research run in this process. This is also what an app-started worker runs.

    The process records itself as the run's worker (refusing if another live worker has the run) and, on macOS,
    keeps the Mac awake with `caffeinate -is -w <pid>` while it runs (FINRESEARCH_KEEP_AWAKE=false turns it off).
    A failed step does not crash the process: the run is marked failed with the reason and the exit code is 1."""
    import os
    import sys

    from finresearch.api.workers import AWAKE_ENV, WorkerBusy, claim_worker, keep_awake
    from finresearch.orchestrator.ipo import run_until_done

    pipeline = _pipeline(run_id, streams, concurrency)
    try:
        claim_worker(run_id, sys.argv[1:])
    except WorkerBusy as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(2) from e
    if not os.environ.get(AWAKE_ENV):
        keep_awake(os.getpid())
    status = asyncio.run(run_until_done(run_id, wait=wait, pipeline=pipeline, log=console.print))
    console.print(f"run {run_id}: [bold]{status}[/]")
    ipo_status(run_id)
    if status == "failed":
        raise typer.Exit(1)


@ipo_app.command("run")
def ipo_run(
    company: str = typer.Argument(
        ..., help="Company slug (documents are discovered automatically if missing)"
    ),
    name: str | None = typer.Option(None, help="Company name (creates the company if new)"),
    nse_symbol: str | None = typer.Option(None),
    bse_ipo: int | None = typer.Option(
        None, help="BSE IPO number of a BSE-only SME issue (`finresearch ipo bse`)"
    ),
    streams: str | None = typer.Option(None, help="Comma-separated subset of streams (default: all seven)"),
    concurrency: int = typer.Option(4, help="Parallel agents (Max-plan friendly default)"),
    wait: bool = typer.Option(
        False, help="Sleep through Max-window resets and transient-error pauses, resuming automatically"
    ),
) -> None:
    """Start a new IPO research run."""
    from finresearch.db import session_scope
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator.ipo import create_run

    if name or nse_symbol or bse_ipo:
        with session_scope() as db:
            co = get_or_create_company(db, company, name, nse_symbol=nse_symbol)
            if nse_symbol and not co.nse_symbol:
                co.nse_symbol = nse_symbol
            _set_bse_ipo(co, bse_ipo)
    run_id = create_run(company)
    console.print(f"created run {run_id} for {company}")
    _go(run_id, streams, concurrency, wait)


def _set_bse_ipo(co, bse_ipo: int | None) -> None:
    """Mark a company as a BSE SME issue: issue facts, documents and subscription then come from BSE."""
    if bse_ipo:
        co.meta = {**(co.meta or {}), "bse_ipo_no": bse_ipo, "exchange": "BSE"}


@ipo_app.command("bse")
def ipo_bse() -> None:
    """List open and forthcoming BSE SME IPOs with lot size and minimum bid (live from BSE)."""
    from finresearch.adapters.bse import sme_radar

    rows, errors = asyncio.run(sme_radar())
    t = Table(title="BSE SME IPOs")
    for col in (
        "IPO no",
        "symbol",
        "company",
        "status",
        "bidding",
        "price band",
        "lot",
        "min bid",
        "issue shares",
    ):
        t.add_column(col)
    for i, d in rows:
        t.add_row(str(i.ipo_no), (d.symbol if d else None) or i.scrip_code, i.company,
                  {"L": "open", "F": "forthcoming"}.get(i.status or "", i.status or ""),
                  f"{i.issue_start} → {i.issue_end}", i.price_band or "",
                  str(d.market_lot) if d and d.market_lot else "",
                  f"{d.minimum_bid} ({d.min_lots} lots)" if d and d.minimum_bid else "",
                  f"{d.issue_size_shares:,}" if d and d.issue_size_shares else "")  # fmt: skip
    console.print(t)
    for e in errors:
        console.print(f"[yellow]{e}[/]")
    console.print("research one: finresearch ipo run <slug> --name '<company>' --bse-ipo <IPO no>")


@ipo_app.command("harvest")
def ipo_harvest(
    series: str = typer.Option(
        "EQ", help="EQ (mainboard) or SME; SME rows are stored and reported separately"
    ),
    limit: int | None = typer.Option(None, help="Fetch at most this many issues (a trial run)"),
    refresh: bool = typer.Option(False, help="Re-fetch rows that are already complete"),
    delay: float = typer.Option(2.0, min=1.0, help="Seconds between NSE requests (polite: at least 1)"),
    export: Path | None = typer.Option(None, help="Write the whole table to this JSONL file afterwards"),
    import_path: Path | None = typer.Option(
        None, "--import", help="Load rows from a JSONL export instead of NSE"
    ),
    reparse: bool = typer.Option(False, help="Re-derive every row from its stored raw payloads (no network)"),
) -> None:
    """Harvest past NSE issues (final category book, issue info, listing-day prices, Nifty mood) into ipo_history,
    then print the coverage report. Resumable: complete rows are skipped; NSE payloads are disk-cached."""
    import json

    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import IpoHistory
    from finresearch.evals import ipo_history as ih

    if import_path:
        with session_scope() as s:
            console.print(f"imported {ih.import_jsonl(s, import_path)} rows from {import_path}")
    elif reparse:
        with session_scope() as s:
            console.print(f"re-derived {ih.reparse(s)} rows")
    else:
        console.print(asyncio.run(ih.harvest(series=series, limit=limit, refresh=refresh, delay=delay,
                                             log=console.print)))  # fmt: skip
    with session_scope() as s:
        rows = s.scalars(select(IpoHistory)).all()
        console.print_json(json.dumps(ih.coverage(rows), default=str))
        if export:
            console.print(f"exported {ih.export_jsonl(s, export)} rows to {export}")


@ipo_app.command("model")
def ipo_model(
    out: Path | None = typer.Option(None, help="Write the walk-forward report (JSON) here"),
    data: Path | None = typer.Option(None, help="Read a JSONL export instead of the database"),
) -> None:
    """Walk-forward validation of the IPO listing model (logistic + quantile regression) against the base-rate
    table, 2019-2026. Prints AUC, Brier, Brier skill and whether the model passes the bar for use."""
    import json

    from finresearch.evals import ipo_model as im

    rows = im.load_rows(data)
    report = im.build_artefact(rows)
    console.print_json(json.dumps(im.summary(report), default=str))
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1, default=str) + "\n")
        console.print(f"wrote {out}")


stock_app = typer.Typer(no_args_is_help=True, help="Listed-stock research reports (multi-agent pipeline)")
app.add_typer(stock_app, name="stock")


@stock_app.command("peers")
def stock_peers_now(
    symbols: str = typer.Option(
        "",
        help="comma-separated NSE symbols (default: the NIFTY 500 plus held/watched stocks outside it and their "
        "industry peers)",
    ),
) -> None:
    """Build the stock peer dataset now (the monitor does this every weekday evening): ~1,000 polite NSE requests
    for the NIFTY 500, about 15-20 minutes, plus a bounded few hundred for held/watched stocks outside it."""
    from finresearch.signals import stock_peers

    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] or None
    res = asyncio.run(stock_peers.refresh(symbols=syms))
    console.print(f"peer dataset as of {res['as_of']}: {res['stocks']} stocks, {res['failed']} failed: "
                  f"{stock_peers.store_path()}")  # fmt: skip


@stock_app.command("run")
def stock_run(
    company: str = typer.Argument(
        ..., help="Company slug (annual reports and results are discovered if missing)"
    ),
    name: str | None = typer.Option(None, help="Company name (creates the company if new)"),
    nse_symbol: str | None = typer.Option(None),
    bse_code: str | None = typer.Option(
        None, help="BSE scrip code of a BSE-only stock (e.g. 526433); its data and filings are read from BSE"
    ),
    streams: str | None = typer.Option(None, help="Comma-separated subset of the six stock streams"),
    concurrency: int = typer.Option(4, help="Parallel agents (Max-plan friendly default)"),
    wait: bool = typer.Option(
        False, help="Sleep through Max-window resets and transient-error pauses, resuming automatically"
    ),
) -> None:
    """Start a new listed-stock research run (resume any run with `finresearch research resume <run_id>`)."""
    from finresearch.db import session_scope
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator.base import create_run

    if name or nse_symbol or bse_code:
        with session_scope() as db:
            co = get_or_create_company(db, company, name, nse_symbol=nse_symbol)
            if nse_symbol and not co.nse_symbol:
                co.nse_symbol = nse_symbol
            if bse_code and not co.bse_code:
                co.bse_code = bse_code
    run_id = create_run(company, kind="stock_report")
    console.print(f"created stock run {run_id} for {company}")
    _go(run_id, streams, concurrency, wait)


research_app = typer.Typer(no_args_is_help=True, help="Any research run (IPO, stock, fund, bond)")
app.add_typer(research_app, name="research")


@research_app.command("resume")
def research_resume(
    run_id: int, streams: str | None = typer.Option(None, help="Default: the streams the run started with"),
    concurrency: int | None = typer.Option(None, help="Default: the concurrency the run started with"),
    wait: bool = typer.Option(False),
) -> None:  # fmt: skip
    """Resume a paused or failed run of any kind; finished steps are not repeated."""
    _go(run_id, streams, concurrency, wait)


@research_app.command("status")
def research_status(run_id: int) -> None:
    """Show a run's steps, costs and Max-window usage."""
    ipo_status(run_id)


fund_app = typer.Typer(no_args_is_help=True, help="Mutual-fund research reports (multi-agent pipeline)")
app.add_typer(fund_app, name="fund")


@fund_app.command("rank")
def fund_rank_now() -> None:
    """Rank every fund within its SEBI category now (the monitor does this daily). The first run reads up to 61
    AMFI month-end NAV snapshots, one a second; later runs reuse them from disk."""
    from finresearch.signals import fund_rank

    res = asyncio.run(fund_rank.refresh())
    console.print(f"ranked {res['funds']} funds in {res['categories']} categories as of {res['as_of']}: "
                  f"{fund_rank.store_path()}")  # fmt: skip


@fund_app.command("run")
def fund_run(
    scheme_code: str = typer.Argument(..., help="AMFI scheme code (find it with `finresearch fund search`)"),
    streams: str | None = typer.Option(None, help="Comma-separated subset of the six fund streams"),
    concurrency: int = typer.Option(4, help="Parallel agents (Max-plan friendly default)"),
    wait: bool = typer.Option(
        False, help="Sleep through Max-window resets and transient-error pauses, resuming automatically"
    ),
) -> None:
    """Start a new mutual-fund research run for an AMFI scheme."""
    from finresearch.orchestrator.base import create_run

    slug = asyncio.run(_ensure_scheme(scheme_code))
    run_id = create_run(slug, kind="fund_report")
    console.print(f"created fund run {run_id} for {slug}")
    _go(run_id, streams, concurrency, wait)


@fund_app.command("search")
def fund_search(query: str) -> None:
    """Find mutual-fund schemes in AMFI's NAV file (code, plan, option, category, latest NAV)."""
    from finresearch.mcp_server.server import amfi_scheme_search

    for r in json.loads(asyncio.run(amfi_scheme_search(query))):
        console.print(f"{r['scheme_code']:>7}  {r['name']} · {r['plan']} · {r['option']} · {r['category']} · "
                      f"NAV {r['nav']} ({r['nav_date']})")  # fmt: skip


async def _ensure_scheme(scheme_code: str) -> str:
    from finresearch.orchestrator.fund import ensure_scheme_company

    try:
        return (await ensure_scheme_company(scheme_code))["slug"]
    except LookupError as e:
        raise typer.BadParameter(str(e)) from e


bond_app = typer.Typer(no_args_is_help=True, help="Listed bond / NCD research reports (multi-agent pipeline)")
app.add_typer(bond_app, name="bond")


@bond_app.command("search")
def bond_search(query: str) -> None:
    """Find listed bonds / NCDs traded on NSE by symbol fragment or ISIN."""
    from finresearch.mcp_server.server import nse_bond_search

    for r in json.loads(asyncio.run(nse_bond_search(query))):
        console.print(f"{r['isin']}  {r['symbol']:<12} {r['series'] or '':<3} coupon {r['coupon_pct']}%  "
                      f"LTP {r['last_price']}  matures {r['maturity']}  {r['rating'] or 'unrated'} "
                      f"{'; '.join(r['warnings'])}")  # fmt: skip


@bond_app.command("run")
def bond_run(
    isin: str,
    streams: str | None = typer.Option(None, help="Comma-separated subset of the five bond streams"),
    concurrency: int = typer.Option(4),
    wait: bool = typer.Option(
        False, help="Sleep through Max-window resets and transient-error pauses, resuming automatically"
    ),
) -> None:
    """Start a new listed-bond research run for an ISIN."""
    from finresearch.orchestrator.base import create_run
    from finresearch.orchestrator.bond import ensure_bond_company

    try:
        slug = asyncio.run(ensure_bond_company(isin))["slug"]
    except LookupError as e:
        raise typer.BadParameter(str(e)) from e
    run_id = create_run(slug, kind="bond_report")
    console.print(f"created bond run {run_id} for {slug}")
    _go(run_id, streams, concurrency, wait)


@ipo_app.command("resume")
def ipo_resume(
    run_id: int,
    streams: str | None = typer.Option(None, help="Default: the streams the run started with"),
    concurrency: int | None = typer.Option(None, help="Default: the concurrency the run started with"),
    wait: bool = typer.Option(False),
) -> None:
    """Resume a paused or failed run; finished steps are not repeated."""
    _go(run_id, streams, concurrency, wait)


@ipo_app.command("render")
def ipo_render(
    run_id: int, pdf: bool = typer.Option(True, help="Also render the PDF (needs Chrome)")
) -> None:
    """(Re)build the research folder pack for a run: report md/html/pdf, tables, charts, documents."""
    from finresearch.render.pack import render_pack

    r = render_pack(run_id, pdf=pdf)
    state = "[green]PASSED[/]" if r.gate_ok else "[red]BLOCKED (draft only)[/]"
    console.print(f"publish gate: {state} · {r.files} files · pdf pages: {r.pdf_pages} → {r.path}")
    for b in r.blocking[:10]:
        console.print(f"  [red]blocking[/] {b[:200]}")
    for n in r.notes:
        console.print(f"  [yellow]note[/] {n}")


@eval_app.command("export")
def eval_export(run_id: int, out: Path = typer.Option(..., help="Fixture JSON to write")) -> None:
    """Export a run's ledger, step statistics and report to a text fixture (no documents)."""
    from finresearch.db import session_scope
    from finresearch.evals.replay import export_run

    with session_scope() as s:
        data = export_run(s, run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n")
    console.print(
        f"{len(data['claims'])} claims, {len(data['steps'])} steps → {out} ({out.stat().st_size:,} bytes)"
    )


@eval_app.command("replay")
def eval_replay(
    fixture: Path, company: str | None = typer.Option(None, help="Gold set (default: fixture company)")
) -> None:
    """Import a fixture as a new run and score it against its gold set (no Claude, no documents)."""
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep
    from finresearch.evals.gold import evaluate, load_gold
    from finresearch.evals.replay import import_run

    data = json.loads(fixture.read_text())
    with session_scope() as s:
        run_id = import_run(s, data, slug_suffix=f"-replay-{int(time.time())}")
        report = (s.query(AgentStep).filter_by(run_id=run_id, stage="synthesis", status="done")
                  .order_by(AgentStep.finished_at.desc()).first().output or {}).get("report_markdown")  # fmt: skip
        res = evaluate(s, run_id, load_gold(company or data["company"]["slug"]), report)
    console.print(res.markdown())


@eval_app.command("backtest")
def eval_backtest() -> None:
    """Compare every finished IPO report's verdict with the listing outcome recorded by the monitor or journal."""
    from finresearch.db import session_scope
    from finresearch.evals.backtest import backtest, markdown

    with session_scope() as s:
        console.print(markdown(backtest(s)))


@eval_app.command("gold")
def eval_gold(
    run_id: int, company: str | None = typer.Option(None, help="Gold set name (default: the run's company)")
) -> None:
    """Score a run against its company's gold set; prints a markdown summary to paste into PRs."""
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, Company, ResearchRun
    from finresearch.evals.gold import evaluate, load_gold

    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None:
            raise typer.BadParameter(f"unknown run {run_id}")
        slug = company or s.get(Company, run.company_id).slug
        synth = (
            s.query(AgentStep)
            .filter_by(run_id=run_id, stage="synthesis", status="done")
            .order_by(AgentStep.finished_at.desc(), AgentStep.id.desc())
            .first()
        )
        res = evaluate(
            s, run_id, load_gold(slug), (synth.output or {}).get("report_markdown") if synth else None
        )
    md = res.markdown()
    out = get_settings().reports_dir.parent / "evals" / f"{slug}-run{run_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    console.print(md)
    console.print(f"saved {out}")


@ipo_app.command("status")
def ipo_status(run_id: int) -> None:
    """Show a run's steps, costs and Max-window usage."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, ResearchRun

    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None:
            raise typer.BadParameter(f"unknown run {run_id}")
        t = Table(
            title=f"run {run_id} — {run.status}"
            + (f" (resume after {run.resume_after})" if run.resume_after else "")
        )
        for col in ("step", "status", "tier/model", "turns", "secs", "5h before→after", "est $"):
            t.add_column(col)
        for st in s.scalars(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.id)):
            util = (
                f"{st.five_hour_before or 0:.0%}→{st.five_hour_after:.0%}"
                if st.five_hour_after is not None
                else ""
            )
            t.add_row(
                st.key,
                st.status,
                f"{st.tier or ''} {st.model or ''}".strip(),
                str(st.num_turns or ""),
                f"{st.duration_s or 0:.0f}",
                util,
                f"{st.cost_usd_est or 0:.2f}",
            )
        console.print(t)
        m = run.manifest or {}
        if run.status == "paused" and m.get("pause_reason"):
            console.print(f"[yellow]paused:[/] {m['pause_reason']}")
        if run.status == "failed" and m.get("last_error"):
            err = m["last_error"]
            console.print(f"[red]failed:[/] {err.get('message') if isinstance(err, dict) else err}")


@app.command()
def serve(
    port: int = typer.Option(8710, help="Port on 127.0.0.1"),
    monitor: bool = typer.Option(True, help="Run the monitoring scheduler inside the API process"),
    log_file: Path | None = typer.Option(
        None,
        help="Write logs to this file, rotated by size (e.g. data/logs/serve.log); default: the terminal",
    ),
) -> None:
    """Start the local API for the research app (always bound to 127.0.0.1). The first start creates the local API
    token (Keychain + data/state/api_token, mode 0600) that every route but /api/health requires."""
    import uvicorn

    from finresearch.api import create_app
    from finresearch.api.auth import bootstrap, token_file

    token = bootstrap()
    console.print(
        f"API token: {token_file()} (0600; the web app reads it, CLI clients send it as a bearer token)"
    )
    if log_file is None:
        uvicorn.run(
            create_app(monitor=monitor, api_token=token), host="127.0.0.1", port=port, log_level="info"
        )
        return
    from finresearch.logfiles import uvicorn_log_config

    uvicorn.run(create_app(monitor=monitor, api_token=token), host="127.0.0.1", port=port, log_level="info",
                log_config=uvicorn_log_config(log_file))  # fmt: skip


@app.command()
def logpipe(path: Path = typer.Argument(..., help="Log file to write, rotated by size")) -> None:
    """Copy stdin into a size-capped, rotated log file (for the web app's output; finresearch.logfiles)."""
    from finresearch.logfiles import stdin_pipe

    stdin_pipe(path)


@app.command()
def prune() -> None:
    """Run the retention policy now: expired HTTP cache entries and old intraday series (monitor.retention).
    Never touches research packs, reports, backups, documents or run transcripts."""
    from datetime import UTC, datetime

    from finresearch.monitor.retention import prune as run

    res = run(datetime.now(UTC))
    console.print(res)


secrets_app = typer.Typer(
    no_args_is_help=True, help="Secrets in the macOS Keychain (the database keeps references)"
)
app.add_typer(secrets_app, name="secrets")


def _secret_report(rep, verb: str) -> None:
    counts = rep.counts()
    for table in ("broker_connection", "notification_setting"):
        fields = sorted({f"{p.key}.{p.field}" for p in rep.found if p.table == table})
        console.print(f"{table}: {counts.get(table, 0)} plaintext secret field(s) {verb}"
                      + (f" ({', '.join(fields)})" if fields else ""))  # fmt: skip
    if rep.unknown:
        console.print(
            f"[yellow]broker_connection rows of unknown connectors (not classified): {', '.join(rep.unknown)}"
        )


@secrets_app.command("check")
def secrets_check() -> None:
    """Exit 1 if any secret is still stored as plain text in the database (counts and field names only)."""
    from finresearch.db import session_scope
    from finresearch.secrets_migrate import scan

    with session_scope() as s:
        rep = scan(s)
    _secret_report(rep, "left")
    if rep.found or rep.unknown:
        console.print(
            "[red bold]plaintext secrets in the database: run `uv run finresearch secrets migrate --apply`"
        )
        raise typer.Exit(1)
    console.print("[green]no plaintext secrets in the database")


@secrets_app.command("migrate")
def secrets_migrate(
    apply: bool = typer.Option(False, "--apply", help="Move them (default: a dry run that only counts)"),
) -> None:
    """Move plaintext secrets from the database into the Keychain and keep references. Idempotent; prints counts and
    field names only, never a value."""
    from finresearch.db import get_engine, session_scope
    from finresearch.secrets import backend
    from finresearch.secrets_migrate import migrate, vacuum

    with session_scope() as s:
        rep = migrate(s, apply=apply)
    _secret_report(rep, "moved" if apply else "would be moved")
    console.print(f"secret store: {backend().name}")
    if not apply:
        console.print("dry run: nothing changed (add --apply)")
        return
    if rep.moved:
        vacuum(get_engine())  # the old row versions still held the plaintext in the table files
    console.print(f"[green]moved {rep.moved} secret field(s)")


monitor_app = typer.Typer(
    no_args_is_help=True, help="Monitoring after the report: subscription, listing, lock-ins"
)
app.add_typer(monitor_app, name="monitor")


@monitor_app.command("watch")
def monitor_watch(
    company: str,
    name: str | None = typer.Option(None, help="Company name (creates the company if new)"),
    bse_ipo: int | None = typer.Option(
        None, help="BSE IPO number of a BSE-only SME issue (`finresearch ipo bse`)"
    ),
) -> None:
    """Start (or refresh) monitoring a company's IPO from NSE's issue information (BSE's for a BSE SME issue)."""
    from finresearch.monitor.watch import watch_company

    if bse_ipo:
        from finresearch.db import session_scope
        from finresearch.ingest.documents import get_or_create_company

        with session_scope() as db:
            _set_bse_ipo(get_or_create_company(db, company, name), bse_ipo)

    w = asyncio.run(watch_company(company))
    console.print(f"watching {w['nse_symbol']}: bidding {w['open_date']} → {w['close_date']}, allotment "
                  f"{w['allotment_date']}, listing {w['listing_date']} (expected)")  # fmt: skip


@monitor_app.command("holidays")
def monitor_holidays(force: bool = typer.Option(True, help="Fetch even if the cache is fresh")) -> None:
    """Fetch NSE's trading and settlement holidays for the current year into the cache."""
    from finresearch.adapters.nse_holidays import load_holidays, refresh_holidays

    asyncio.run(refresh_holidays(force=force))
    for kind in ("trading", "clearing"):
        rows = load_holidays(kind)
        console.print(
            f"{kind}: {len(rows)} holidays, e.g. "
            + ", ".join(f"{d} {n}" for d, n in sorted(rows.items())[:3])
        )


@monitor_app.command("tick")
def monitor_tick() -> None:
    """Run one monitoring pass now (plan slots, run due checks)."""
    from finresearch.monitor.jobs import Deps
    from finresearch.monitor.scheduler import drain, tick

    async def once():
        out = await tick(Deps.live())
        await drain()  # the daily portfolio pass runs in the background inside `serve`; here, wait for it
        return out

    console.print(asyncio.run(once()))


@monitor_app.command("run")
def monitor_run(interval: float = typer.Option(60, help="Seconds between passes")) -> None:
    """Run the monitoring loop in the foreground (use this when the API is not running)."""
    from finresearch.monitor.scheduler import run_forever

    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever(interval_s=interval))


if __name__ == "__main__":
    app()


forecasts_app = typer.Typer(
    no_args_is_help=True, help="Forecast ledger: logged probabilities and calibration"
)
app.add_typer(forecasts_app, name="forecasts")


@forecasts_app.command("backfill")
def forecasts_backfill() -> None:
    """Log forecasts for finished IPO and stock reports that have none yet (the monitor also does this daily)."""
    from finresearch.db import session_scope
    from finresearch.signals.ledger import backfill_runs

    with session_scope() as s:
        ids = backfill_runs(s)
    console.print(f"{len(ids)} forecast(s) logged or refreshed: {ids}")


@forecasts_app.command("resolve")
def forecasts_resolve() -> None:
    """Resolve every due forecast now (NSE, politely), as the monitor does after the close."""
    from finresearch.monitor.jobs import Deps
    from finresearch.signals.ledger import resolve_due

    console.print(asyncio.run(resolve_due(Deps.live())))


@forecasts_app.command("calibration")
def forecasts_calibration() -> None:
    """Brier score, skill vs the base rate and hit rate (Wilson 95 % CI) per asset and method."""
    from finresearch.db import session_scope
    from finresearch.signals.ledger import calibration_groups

    with session_scope() as s:
        for g in calibration_groups(s):
            console.print(g)


portfolio_app = typer.Typer(
    no_args_is_help=True, help="Your portfolio: import statements and tradebooks (local only)"
)
app.add_typer(portfolio_app, name="portfolio")


def _import_file(res, path: Path, content: bytes, dry_run: bool) -> None:
    import hashlib

    from finresearch.db import session_scope
    from finresearch.portfolio.service import apply, preview, save_upload

    sha = hashlib.sha256(content).hexdigest()
    with session_scope() as s:
        if dry_run or res.holdings_only:
            out = preview(s, res)
        else:
            from sqlalchemy import select

            from finresearch.db.models import PortfolioImport

            if s.scalar(select(PortfolioImport.id).where(PortfolioImport.sha256 == sha)):
                raise typer.Exit(console.print("[red]this file was already imported[/red]") or 1)
            out = apply(
                s,
                res,
                filename=path.name,
                sha256=sha,
                saved_path=save_upload(content, sha, path.suffix.lower()),
            )
    for k in ("rows", "new_rows", "added", "duplicates", "reconciled"):
        if k in out:
            console.print(f"{k}: {out[k]}")
    for r in out.get("reconciliation", []):
        mark = "[green]ok[/green]" if r["ok"] else "[red]MISMATCH[/red]"
        console.print(f"  {mark} {r['name']}: statement {r['statement_units']} vs lots {r['lot_units']}")
    for w in out.get("warnings", []):
        console.print(f"  [yellow]{w}[/yellow]")
    if dry_run:
        console.print("dry run: nothing written (add --apply to import)")


@portfolio_app.command("import-cas")
def portfolio_import_cas(
    path: Path, apply_: bool = typer.Option(False, "--apply", help="Write (default: preview)")
) -> None:
    """Import a CAMS/KFintech CAS PDF. The password is asked for (not echoed) and never stored or logged."""
    import getpass

    from finresearch.portfolio.importers import StatementError, parse_cas

    content = path.read_bytes()
    try:
        res = parse_cas(content, getpass.getpass("CAS PDF password (not shown, not stored): "))
    except StatementError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    _import_file(res, path, content, not apply_)


@portfolio_app.command("import-tradebook")
def portfolio_import_tradebook(path: Path, broker: str | None = typer.Option(None, help="zerodha | groww | upstox"),
                               apply_: bool = typer.Option(False, "--apply", help="Write (default: preview)")) -> None:  # fmt: skip
    """Import a broker equity tradebook (CSV or XLSX; the broker is detected from the headers)."""
    from finresearch.portfolio.importers import StatementError, parse_tradebook

    content = path.read_bytes()
    try:
        res = parse_tradebook(content, path.name, broker)
    except StatementError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    _import_file(res, path, content, not apply_)


@portfolio_app.command("tax-csv")
def portfolio_tax_csv(
    out: Path, fy: int | None = typer.Option(None, help="Financial year by its end year, e.g. 2026")
) -> None:
    """Write realised capital gains (one row per FIFO disposal, with the rule applied) as a CSV for your CA."""
    from finresearch.db import session_scope
    from finresearch.portfolio.report import export_rows
    from finresearch.portfolio.tax import export_csv

    with session_scope() as s:
        out.write_text(export_csv(export_rows(s), fy))
    console.print(f"written {out} (a personal estimate: verify with a CA)")


@audit_app.command("prices")
def audit_prices(
    symbols: list[str] = typer.Argument(None, help="NSE symbols or BSE:<code> (default: the audit's list)"),
    index: str = typer.Option("NIFTY 50", help="NSE index to check ('' to skip)"),
    fund: str = typer.Option("122639", help="AMFI scheme code to check ('' to skip)"),
    bond: bool = typer.Option(True, help="Check one traded bond from NSE's list"),
    out: Path | None = typer.Option(None, help="Also write the report (Markdown; .json for JSON)"),
    strict: bool = typer.Option(False, help="Exit 1 when any check is a mismatch"),
) -> None:
    """Cross-check every price source (NSE quote/history/intraday, BSE quote/history/intraday, an index, AMFI NAV, a
    bond) and flag disagreements beyond tolerance. Read-only; a few requests per symbol."""
    import asyncio

    from finresearch.evals.data_audit import audit_live

    report = asyncio.run(
        audit_live(list(symbols or []) or None, index=index or None, fund=fund or None, bond=bond)
    )
    md = report.markdown()
    typer.echo(md)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.as_json(), indent=1) + "\n" if out.suffix == ".json" else md)
    if strict and report.mismatches:
        raise typer.Exit(1)

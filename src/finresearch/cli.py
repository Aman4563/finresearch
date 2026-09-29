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
    from finresearch.orchestrator.ipo import run_until_done

    status = asyncio.run(
        run_until_done(run_id, wait=wait, pipeline=_pipeline(run_id, streams, concurrency), log=console.print)
    )
    console.print(f"run {run_id}: [bold]{status}[/]")
    ipo_status(run_id)


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
    wait: bool = typer.Option(False, help="Sleep through Max-window resets and resume automatically"),
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


stock_app = typer.Typer(no_args_is_help=True, help="Listed-stock research reports (multi-agent pipeline)")
app.add_typer(stock_app, name="stock")


@stock_app.command("run")
def stock_run(
    company: str = typer.Argument(
        ..., help="Company slug (annual reports and results are discovered if missing)"
    ),
    name: str | None = typer.Option(None, help="Company name (creates the company if new)"),
    nse_symbol: str | None = typer.Option(None),
    streams: str | None = typer.Option(None, help="Comma-separated subset of the six stock streams"),
    concurrency: int = typer.Option(4, help="Parallel agents (Max-plan friendly default)"),
    wait: bool = typer.Option(False, help="Sleep through Max-window resets and resume automatically"),
) -> None:
    """Start a new listed-stock research run (resume any run with `finresearch research resume <run_id>`)."""
    from finresearch.db import session_scope
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator.base import create_run

    if name or nse_symbol:
        with session_scope() as db:
            co = get_or_create_company(db, company, name, nse_symbol=nse_symbol)
            if nse_symbol and not co.nse_symbol:
                co.nse_symbol = nse_symbol
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


@fund_app.command("run")
def fund_run(
    scheme_code: str = typer.Argument(..., help="AMFI scheme code (find it with `finresearch fund search`)"),
    streams: str | None = typer.Option(None, help="Comma-separated subset of the six fund streams"),
    concurrency: int = typer.Option(4, help="Parallel agents (Max-plan friendly default)"),
    wait: bool = typer.Option(False, help="Sleep through Max-window resets and resume automatically"),
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
    wait: bool = typer.Option(False, help="Sleep through Max-window resets and resume automatically"),
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


@app.command()
def serve(
    port: int = typer.Option(8710, help="Port on 127.0.0.1"),
    monitor: bool = typer.Option(True, help="Run the monitoring scheduler inside the API process"),
) -> None:
    """Start the local API for the research app (always bound to 127.0.0.1)."""
    import uvicorn

    from finresearch.api import create_app

    uvicorn.run(create_app(monitor=monitor), host="127.0.0.1", port=port, log_level="info")


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
    from finresearch.monitor.scheduler import tick

    console.print(asyncio.run(tick(Deps.live())))


@monitor_app.command("run")
def monitor_run(interval: float = typer.Option(60, help="Seconds between passes")) -> None:
    """Run the monitoring loop in the foreground (use this when the API is not running)."""
    from finresearch.monitor.scheduler import run_forever

    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever(interval_s=interval))


if __name__ == "__main__":
    app()

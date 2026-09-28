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
    agent: bool = typer.Option(True, help="Also search company IR pages with the discovery agent"),
    index: bool = typer.Option(True, help="Build sections, chunks and embeddings"),
) -> None:
    """Find, download and ingest a company's offer and IR documents (NSE, SEBI, IR pages)."""
    from finresearch.db import session_scope
    from finresearch.ingest.discover import discover
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as db:
        co = get_or_create_company(db, company, name, nse_symbol=nse_symbol)
        if nse_symbol and not co.nse_symbol:
            co.nse_symbol = nse_symbol
    rep = asyncio.run(discover(company, use_agent=agent, index=index, log=console.print))
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
def _pipeline(run_id: int, streams: str | None, concurrency: int):
    from finresearch.agents.roles import STREAMS
    from finresearch.orchestrator.ipo import IpoPipeline, PipelineConfig

    chosen = tuple(x.strip() for x in streams.split(",")) if streams else STREAMS
    unknown = set(chosen) - set(STREAMS)
    if unknown:
        raise typer.BadParameter(f"unknown streams {sorted(unknown)}; choose from {STREAMS}")
    cfg = PipelineConfig(
        streams=chosen, concurrency=concurrency, five_hour_ceiling=get_settings().max_five_hour_ceiling
    )
    return IpoPipeline(run_id, config=cfg)


def _go(run_id: int, streams: str | None, concurrency: int, wait: bool) -> None:
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
    streams: str | None = typer.Option(None, help="Comma-separated subset of streams (default: all seven)"),
    concurrency: int = typer.Option(4, help="Parallel agents (Max-plan friendly default)"),
    wait: bool = typer.Option(False, help="Sleep through Max-window resets and resume automatically"),
) -> None:
    """Start a new IPO research run."""
    from finresearch.db import session_scope
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator.ipo import create_run

    if name or nse_symbol:
        with session_scope() as db:
            co = get_or_create_company(db, company, name, nse_symbol=nse_symbol)
            if nse_symbol and not co.nse_symbol:
                co.nse_symbol = nse_symbol
    run_id = create_run(company)
    console.print(f"created run {run_id} for {company}")
    _go(run_id, streams, concurrency, wait)


@ipo_app.command("resume")
def ipo_resume(
    run_id: int,
    streams: str | None = typer.Option(None),
    concurrency: int = typer.Option(4),
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
def serve(port: int = typer.Option(8710, help="Port on 127.0.0.1")) -> None:
    """Start the local API for the research app (always bound to 127.0.0.1)."""
    import uvicorn

    from finresearch.api import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    app()

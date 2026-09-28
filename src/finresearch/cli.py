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


if __name__ == "__main__":
    app()

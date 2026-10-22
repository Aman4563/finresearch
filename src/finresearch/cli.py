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
evidence_app = typer.Typer(no_args_is_help=True, help="Evidence grades of stored claims (no LLM, no network)")
app.add_typer(evidence_app, name="evidence")
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


def usage_by_role(steps) -> list[dict]:
    """Aggregate agent steps per role, costliest first.

    Shares use the CLI's per-step cost estimate (cache-aware, and each step counts only its own calls). The 5-hour
    window delta is shown as an indicator only: parallel streams overlap, so one step's delta includes its
    neighbours' usage. A missing value is left out of its aggregate and counted, never taken as 0.
    """
    by: dict[str, dict] = {}
    for st in steps:
        role = st.role or st.key.split(":", 1)[0]
        u = by.setdefault(role, {"role": role, "steps": 0, "runs": set(), "cost": 0.0, "no_cost": 0, "in_tok": 0,
                                 "out_tok": 0, "turns": 0, "minutes": 0.0, "deltas": []})  # fmt: skip
        u["steps"] += 1
        u["runs"].add(st.run_id)
        if st.cost_usd_est is None:
            u["no_cost"] += 1
        else:
            u["cost"] += st.cost_usd_est
        u["in_tok"] += st.input_tokens or 0
        u["out_tok"] += st.output_tokens or 0
        u["turns"] += st.num_turns or 0
        u["minutes"] += (st.duration_s or 0) / 60
        if st.five_hour_before is not None and st.five_hour_after is not None:
            u["deltas"].append(max(0.0, st.five_hour_after - st.five_hour_before))
    total = sum(u["cost"] for u in by.values())
    rows = []
    for u in by.values():
        d = u.pop("deltas")
        rows.append({**u, "runs": len(u["runs"]), "share": u["cost"] / total if total else None,
                     "mean_5h_delta": sum(d) / len(d) if d else None, "with_5h": len(d)})  # fmt: skip
    return sorted(rows, key=lambda r: r["cost"], reverse=True)


@bridge_app.command()
def usage(
    runs: int = typer.Option(10, help="How many of the latest research runs to include"),
    kind: str | None = typer.Option(None, help="Only this run kind, e.g. ipo_report"),
) -> None:
    """Which agent roles spend the Max window: cost share, tokens, turns and minutes per role across recent runs."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, ResearchRun

    kinds = (kind,) if kind else ("ipo_report", "stock_report", "fund_report", "bond_report")
    with session_scope() as s:
        ids = list(s.scalars(select(ResearchRun.id).where(ResearchRun.kind.in_(kinds))
                             .order_by(ResearchRun.id.desc()).limit(runs)))  # fmt: skip
        steps = list(s.scalars(select(AgentStep).where(AgentStep.run_id.in_(ids)))) if ids else []
        rows = usage_by_role(steps)
    if not rows:
        console.print("no agent steps in the selected runs")
        return
    t = Table(title=f"agent usage by role — {len(ids)} run(s): {min(ids)}–{max(ids)}")
    for col in ("role", "steps", "runs", "share", "est $", "in tok", "out tok", "turns", "min", "mean 5h Δ*"):
        t.add_column(col, justify="left" if col == "role" else "right")
    for r in rows:
        share = f"{r['share']:.0%}" if r["share"] is not None else "?"
        cost = f"{r['cost']:.2f}" + (f" (+{r['no_cost']} ?)" if r["no_cost"] else "")
        delta = f"{r['mean_5h_delta']:.1%} (n={r['with_5h']})" if r["mean_5h_delta"] is not None else "?"
        t.add_row(r["role"], str(r["steps"]), str(r["runs"]), share, cost, f"{r['in_tok']:,}", f"{r['out_tok']:,}",
                  str(r["turns"]), f"{r['minutes']:.0f}", delta)  # fmt: skip
    console.print(t)
    console.print("* window use while the step ran; parallel steps overlap, so it is an upper bound per step. "
                  "Shares use the CLI's per-step cost estimate; input tokens include cache reads.")  # fmt: skip


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
    retry_days: int | None = typer.Option(
        None,
        help="Incremental (the monitor's nightly mode): retry partial rows only if listed in the last N days",
    ),
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
                                             retry_days=retry_days, log=console.print)))  # fmt: skip
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


@bond_app.command("backfill-maturity")
def bond_backfill_maturity(
    offline: bool = typer.Option(False, help="Use only the stored research claims (no NSE request)"),
) -> None:
    """Store the maturity of every library bond that has none (#293): from NSE's bond list, else from the bond
    research's verified NSE maturity claim. A bond found in neither stays unknown; nothing is guessed."""
    from finresearch.db import session_scope
    from finresearch.orchestrator.bond import backfill_maturities

    rows = None
    if not offline:
        from finresearch.adapters.nse_bonds import live_bonds

        try:
            rows = asyncio.run(asyncio.wait_for(live_bonds(), 60))
        except Exception as e:  # the claims still answer; the bonds they do not cover stay unknown
            console.print(
                f"[yellow]NSE's bond list unavailable ({type(e).__name__}); using stored claims only[/]"
            )
    with session_scope() as s:
        res = backfill_maturities(s, rows)
    for r in res:
        console.print(f"{r['slug']:<22} {r['maturity'] or 'unknown':<11} {r['source']}")
    console.print(f"{sum(1 for r in res if r['maturity'])} of {len(res)} library bonds have a known maturity")


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


backup_app = typer.Typer(
    no_args_is_help=True, help="Encrypted off-machine backup copies and restore drills (docs/BACKUPS.md)"
)
app.add_typer(backup_app, name="backup")


def _offsite_dir():
    from finresearch.config import get_settings

    return get_settings().backup_offsite_dir


@backup_app.command("init-key")
def backup_init_key() -> None:
    """Create the 32-byte backup key in the Keychain once (refuses if one exists). Never prints the key."""
    from finresearch.backups.offsite import OffsiteError, init_key

    try:
        ref = init_key()
    except OffsiteError as e:
        console.print(f"[red]{e}")
        raise typer.Exit(1) from None
    console.print(f"[green]backup key created ({ref}); keep a second copy of it (docs/BACKUPS.md)")


@backup_app.command("import-key")
def backup_import_key() -> None:
    """Install an escrowed backup key (base64, typed or piped on stdin, never an argument) on a new Mac."""
    from finresearch.backups.offsite import OffsiteError, import_key

    value = typer.prompt("Backup key (base64)", hide_input=True)
    try:
        ref = import_key(value)
    except OffsiteError as e:
        console.print(f"[red]{e}")
        raise typer.Exit(1) from None
    console.print(f"[green]backup key installed ({ref})")


@backup_app.command("offsite")
def backup_offsite(
    vault: bool = typer.Option(False, "--vault", help="Copy the vault database instead (keeps 2 copies)"),
) -> None:
    """Dump the database and its forward-only tables, encrypt them and copy them to FINRESEARCH_BACKUP_OFFSITE_DIR."""
    from finresearch.backups.offsite import OffsiteError, backup_now

    try:
        made = backup_now(None, _offsite_dir(), kinds=("vault",) if vault else ("db", "tables"))
    except (OffsiteError, RuntimeError) as e:
        console.print(f"[red]offsite: {e}")
        raise typer.Exit(1) from None
    for kind, copy in made.items():
        console.print(f"offsite: {kind} -> {copy.name} ({copy.stat().st_size} bytes, encrypted)")


@backup_app.command("status")
def backup_status() -> None:
    """Exit 1 unless every forward-only table has an off-machine copy at most a day old and a drill passed this
    quarter. An unreadable destination counts as not ok."""
    from finresearch.backups.offsite import status

    st = status(_offsite_dir())
    for t in st["tables"]:
        mark = "[green]ok" if t["ok"] else f"[red]NOT OK: {t['reason']}"
        console.print(f"{t['table']}: copied {t['copied_at'] or 'never'} {mark}")
    d = st["drill"]
    console.print(
        f"restore drill: {d['at'] or 'never'} " + ("[green]ok" if d["ok"] else f"[red]NOT OK: {d['reason']}")
    )
    if not st["ok"]:
        raise typer.Exit(1)


@backup_app.command("drill")
def backup_drill(
    kind: str = typer.Option("tables", help="Which copy to restore: tables | db | vault"),
    scratch: str | None = typer.Option(
        None, help="New scratch database (must end in _test; default <db>_drill_test)"
    ),
    keep_db: bool = typer.Option(False, "--keep-db", help="Leave the scratch database for inspection"),
) -> None:
    """Restore the newest off-machine copy into a NEW scratch *_test database and compare row counts with its
    manifest; the result is logged at the destination. Exit 1 on a failed drill, 2 on a refused target."""
    from finresearch.backups.drill import DrillRefused, run
    from finresearch.backups.offsite import OffsiteError

    try:
        res = run(_offsite_dir(), kind=kind, scratch=scratch, keep_db=keep_db)
    except DrillRefused as e:
        console.print(f"[red]drill refused: {e}")
        raise typer.Exit(2) from None
    except OffsiteError as e:
        console.print(f"[red]drill: {e}")
        raise typer.Exit(1) from None
    if res.get("skipped"):
        console.print(f"[red]drill: {res['skipped']}")
        raise typer.Exit(1)
    for t, n in sorted((res["expected"] or {}).items()):
        got = (res["restored"] or {}).get(t)
        console.print(f"{t}: manifest {n}, restored {got}" + ("" if got == n else " [red]MISMATCH"))
    if not res["ok"]:
        console.print(f"[red]drill FAILED: {res['error'] or 'row counts differ'}")
        raise typer.Exit(1)
    console.print(f"[green]drill passed: {res['copy']} restored into {res['scratch']}")


batch_app = typer.Typer(
    no_args_is_help=True, help="Research batches outside market hours (finresearch.batchwindow)"
)
app.add_typer(batch_app, name="batch")


@batch_app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def batch_run(
    cmd: list[str] = typer.Argument(..., help="The command, after --"),
    wait: bool = typer.Option(
        False, "--wait", help="Sleep until 15:45 IST instead of exiting 75 when deferred"
    ),
    cpu_seconds: int | None = typer.Option(None, help="Hard CPU-time cap for the job (RLIMIT_CPU)"),
) -> None:
    """Run a research batch (backfill, fit, bootstrap, simulation) only outside 09:00-15:45 IST on trading days,
    at background CPU/I-O priority, paused while the market is open. Exit 75 when deferred, else the job's code."""
    from finresearch.batchwindow import run_batch

    res = run_batch(cmd, wait=wait, cpu_seconds=cpu_seconds)
    if res.deferred:
        console.print(f"batch: deferred during market hours; start again after {res.resume_at:%H:%M} IST")
        raise typer.Exit(75)
    if res.paused_s:
        console.print(f"batch: paused {res.paused_s / 60:.0f} min while the market was open")
    raise typer.Exit(res.returncode or 0)


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


@forecasts_app.command("cards")
def forecasts_cards() -> None:
    """Record every logged F&O card (card_log) that has no forecast yet (#376; the monitor also does this hourly)."""
    from finresearch.db import session_scope
    from finresearch.fnobook import card_forecasts

    with session_scope() as s:
        n = card_forecasts.record_pending(s)
    console.print(f"{n} card forecast(s) recorded")


@forecasts_app.command("resolve")
def forecasts_resolve(
    recheck: bool = typer.Option(
        False, help="Also try forecasts checked in the last 6 hours (e.g. after a harvest)"
    ),
) -> None:
    """Resolve every due forecast now (NSE, politely), as the monitor does after the close."""
    from finresearch.monitor.jobs import Deps
    from finresearch.signals.ledger import resolve_due

    console.print(asyncio.run(resolve_due(Deps.live(), recheck=recheck)))


@forecasts_app.command("calibration")
def forecasts_calibration() -> None:
    """Brier score, skill vs the base rate and hit rate (Wilson 95 % CI) per asset and method."""
    from finresearch.db import session_scope
    from finresearch.signals.ledger import calibration_groups

    with session_scope() as s:
        for g in calibration_groups(s):
            console.print(g)


@forecasts_app.command("horse-race")
def forecasts_horse_race(
    window: str = typer.Option(
        "forward", help="forward (primary), seen, tuesday or design (horserace/specs.py)"
    ),
    json_out: Path | None = typer.Option(None, "--json", help="Also write the full report here"),
    source: str = typer.Option(
        "ledger", help="ledger (the card ledger) or replay (2022-26 bhavcopy, addendum A1)"
    ),
) -> None:
    """The pre-registered card horse race (#377) on the card ledger's resolved cards, or (--source replay) on one card
    per stored session replayed from the bhavcopy: every forecaster's Brier, log score, CRPS, tail coverage and DM
    against the incumbent, and the adoption gate. Read only; refuses if horserace/specs.py or the replay's addendum
    changed after registration (horserace/prereg.py)."""
    import json

    from rich.table import Table

    from finresearch.db import session_scope
    from finresearch.horserace import ledger_events, race, replay

    if source not in ("ledger", "replay"):
        console.print(f"unknown source {source!r}: ledger or replay")
        raise typer.Exit(2)
    try:
        race.require_registered() if source == "ledger" else replay.require_registered()
    except race.PreregistrationError as e:
        console.print(str(e))
        raise typer.Exit(1) from e
    if window not in race.specs.WINDOWS:
        console.print(f"unknown window {window!r}: one of {', '.join(race.specs.WINDOWS)}")
        raise typer.Exit(2)
    if source == "replay":
        from finresearch.backtest.dbhistory import DbHistory

        try:
            rep = replay.run_window(DbHistory(), window)
        except ValueError as e:
            console.print(str(e))
            raise typer.Exit(2) from e
        rp = rep["replay"]
        console.print(f"replay ({rp['addendum']}): {rp['cards']} card(s) from {rp['first_origin']} to "
                      f"{rp['last_origin']}; {rp['counts']}; skips {rp['skips']}")  # fmt: skip
        console.print(f"challenger p_rw_bus ({rp['addendum_a2']}): T_bus unknown on {rp['t_bus_unknown']} card(s); "
                      f"weekdays without a stored session (holidays or missing days): {rp['calendar_gap_weekdays']}")  # fmt: skip
        console.print(f"challenger p_rn_b76 ({rp['addendum_a3']}): unknown on {rp['p_rn_b76_unknown']} card(s); "
                      f"reasons {rp['p_rn_b76_reasons']}")  # fmt: skip
    else:
        with session_scope() as s:
            rep = race.run(ledger_events.load(s), window)
    console.print(f"window {window}: {rep['n_events']} event(s); {rep['window_role']}")
    t = Table("forecaster", "underlying", "n", "missing", "Brier", "log", "CRPS", "DM p (Holm)", "tails")
    for r in race.summary_rows(rep):
        t.add_row(*r)
    console.print(t)
    console.print(f"adopted: {', '.join(rep['adopted']) or 'none'}")
    if json_out:
        json_out.write_text(json.dumps(rep, indent=1, default=str))
        console.print(f"report written to {json_out}")


@forecasts_app.command("skill")
def forecasts_skill(
    json_out: Path | None = typer.Option(None, "--json", help="Also write the full report here"),
) -> None:
    """The live skill monitor (#378) on read: per method x event kind x underlying, calibration-in-the-large and the
    promotion BSS CI against the benchmark; at >= 100 resolved independent events also Spiegelhalter, Murphy and the
    Bernoulli CUSUM, with alarms. Read only; refuses if skillmon/specs.py changed after its registration."""
    import json

    from rich.table import Table

    from finresearch.db import session_scope
    from finresearch.fincalc.dates import now_ist
    from finresearch.skillmon import monitor

    try:
        with session_scope() as s:
            rep = monitor.report(s, now_ist())
    except monitor.PreregistrationError as e:
        console.print(str(e))
        raise typer.Exit(1) from e

    def f(x: float | None, d: int = 3) -> str:
        return "—" if x is None else f"{x:+.{d}f}"

    t = Table("cell", "status", "events", "CITL", "BSS [CI]", "promotion", "alarms")
    for c in rep["cells"]:
        sk = c["skill"]
        bss = (
            sk["status"]
            if sk["status"] == "no benchmark"
            else f"{f(sk.get('bss'))} [{f(sk.get('lo'))}, {f(sk.get('hi'))}]"
        )
        t.add_row(c["label"], c["status"], f"{c['independent']} of {c['trigger']}", f(c["citl"]), bss,
                  sk.get("promotion", "—"), "; ".join(a["text"] for a in c["alarms"]) or "—")  # fmt: skip
    console.print(t)
    console.print(f"{rep['version']}: {rep['counts']}; {rep['alarms']} alarm(s)")
    if json_out:
        json_out.write_text(json.dumps(rep, indent=1, default=str))
        console.print(f"report written to {json_out}")


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


@evidence_app.command("regrade")
def evidence_regrade(
    apply: bool = typer.Option(
        False, "--apply/--dry-run", help="Write the changes (default: dry run, writes nothing)"
    ),
    all_d: bool = typer.Option(
        False, "--all", help="Re-check every grade-D citation, not only those saved before #265"
    ),
    reason: str | None = typer.Option(None, help="Why, recorded on each changed citation (default: #285)"),
) -> None:
    """Re-check grade-D (fincalc) citations saved before #265: run the stored call again and bind every argument to a
    cited input claim or declared constant. Prints each citation (still D / now U with the reason) and every report
    whose publish gate changes. --apply writes all changes in one transaction, each stamped with who/when/why and its
    previous fields (computation.regrade.before) so it can be reverted; running it again changes nothing."""
    from finresearch.db import session_scope
    from finresearch.verify.regrade import DEFAULT_REASON, regrade

    with session_scope() as s:
        res = regrade(s, all_d=all_d, apply=apply, reason=reason or DEFAULT_REASON)
        if not apply:
            s.rollback()
    if apply:  # each written change is also printed (the stamp on the row is the durable audit record)
        for r in res.changed:
            console.print(f"applied: run {r.run_id} C{r.claim_id} citation {r.citation_id} -> {r.grade}")
    t = Table(title=f"grade-D citations re-checked ({'APPLIED' if apply else 'dry run, nothing written'})")
    for col in ("run", "claim", "citation", "importance", "result", "reason"):
        t.add_column(col)
    for r in res.citations:
        result = ("still D" if r.grade == "D" else "[red]downgraded to U[/]") if r.changed else "unchanged"
        t.add_row(str(r.run_id), f"C{r.claim_id}", str(r.citation_id), r.importance, result, r.reason[:160])
    console.print(t)
    down = sum(1 for r in res.changed if r.grade == "U")
    console.print(f"{len(res.citations)} checked · {len(res.changed)} changed ({down} downgraded to U)")
    label = {True: "PASSED", False: "BLOCKED", None: "no report"}
    for g in res.gates:
        if g.changes:
            console.print(f"run {g.run_id}: publish gate {label[g.before_ok]} -> [red]{label[g.after_ok]}[/]")
            for b in g.new_blocking[:10]:
                console.print(f"  [red]blocking[/] {b[:200]}")
        else:
            console.print(f"run {g.run_id}: publish gate unchanged ({label[g.after_ok]})")
    if not apply and res.changed:
        console.print("dry run: re-run with --apply to write these changes (back up the database first)")


inbox_app = typer.Typer(no_args_is_help=True, help="The statement inbox folder (CAS PDFs, tradebooks)")
app.add_typer(inbox_app, name="inbox")
MAIL_SCRIPT = "save_cas_attachments.applescript"


@inbox_app.command("setup-mail")
def inbox_setup_mail(
    script: bool = typer.Option(
        False, "--script", help="Print only the AppleScript, with your inbox folder filled in"
    ),
) -> None:
    """How to let Apple Mail save CAS emails' PDFs into the statement inbox (#296). Prints the steps and the inbox
    path; with --script, the rule script to save. Mail itself is never touched: you install the rule."""
    from finresearch.config import REPO_ROOT
    from finresearch.portfolio.connectors.inbox import ensure_dirs

    root = ensure_dirs()  # the folder must exist (0700) before Mail saves into it
    src = (REPO_ROOT / "scripts" / "mail" / MAIL_SCRIPT).read_text()
    if '"' in str(root) or "\\" in str(root):
        raise typer.BadParameter(f"the inbox path {root} contains a quote or backslash: move the data folder")
    if script:
        typer.echo(src.replace("__FINRESEARCH_INBOX__", str(root)), nl=False)
        return
    dest = Path.home() / "Library" / "Application Scripts" / "com.apple.mail" / MAIL_SCRIPT
    typer.echo(f"""Statement inbox: {root}

1. Save the rule script where Mail looks for rule scripts (one command, in your own Terminal):
     mkdir -p "{dest.parent}" && cd "{REPO_ROOT}" && uv run finresearch inbox setup-mail --script > "{dest}"
2. Mail -> Settings -> Rules -> Add Rule. Name: FinResearch CAS. "If ANY of the following conditions are met"
   (the script itself re-checks that the address's domain is exactly one of these):
     From  contains  camsonline.com
     From  contains  kfintech.com
     From  contains  nsdl.co.in
     From  contains  nsdl.com
     From  contains  cdslstatement.com
     From  contains  cdslindia.com
   Perform: Run AppleScript -> {MAIL_SCRIPT}. Click OK; choose "Don't Apply" for existing mail (or Apply to
   drop the statements you already have into the inbox).
3. Profile -> Connections -> Statement inbox: turn it on and save the PDF passwords (the CAS password you chose on
   the mailback form, and the PAN for NSDL/CDSL e-CAS). They go to the Keychain; PDFs without a working password
   wait in the folder.
4. Check: the next CAS email leaves a file named like camsonline.com-<date>-1.pdf in the folder (owner-only, 0600)
   and the inbox imports it within 5 minutes (Profile -> Connections shows the log).

The script re-checks the sender domain and saves PDFs only. Mail does not verify who sent a message, so the domain
list is a filter, not proof; the app still opens a PDF only with your saved passwords. To stop: delete the rule.
Details: docs/BROKER_SETUP.md, "CAS emails straight into the inbox".""")


fno_app = typer.Typer(
    no_args_is_help=True, help="Historical F&O data: NSE's F&O bhavcopy backfill and IV history"
)
app.add_typer(fno_app, name="fno")


@fno_app.command("backfill")
def fno_backfill(
    start: str = typer.Option(..., "--from", help="First day, YYYY-MM-DD (e.g. 2022-01-01)"),
    end: str | None = typer.Option(None, "--to", help="Last day (default today, IST)"),
    symbols: str = typer.Option(
        "", help="Extra stock underlyings for options, comma-separated (held/watched always)"
    ),
    delay: float = typer.Option(
        0.5, min=0.0, help="Extra seconds between downloads, on top of the shared NSE limit"
    ),
    keep_raw: bool = typer.Option(
        True, help="Keep each day's zip under data/fno_bhav/ (re-filtering needs no download)"
    ),
) -> None:
    """Download and store NSE's daily F&O bhavcopy for [from, to]: index options (NIFTY, BANKNIFTY, FINNIFTY,
    MIDCPNIFTY), held/watched stock options and every future, then the derived IV series. Resumable and idempotent:
    loaded days are skipped, failed days retried; re-run the same command to continue."""
    from datetime import date as _date

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    from finresearch.fnohist import store

    extra = [x.strip().upper() for x in symbols.split(",") if x.strip()]
    stocks = sorted({*store.tracked_stocks(), *extra})
    console.print(f"stock underlyings: {', '.join(stocks) or 'none'}")
    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(),
                  console=console) as bar:  # fmt: skip
        task = bar.add_task("F&O bhavcopy", total=None)

        def progress(i: int, n: int, day, status: str) -> None:
            bar.update(task, total=n, completed=i, description=f"F&O bhavcopy {day} {status}")

        res = asyncio.run(store.backfill(_date.fromisoformat(start), _date.fromisoformat(end) if end else None,
                                         stocks=stocks, pause=delay, keep_raw=keep_raw, progress=progress))  # fmt: skip
    console.print_json(json.dumps({k: v for k, v in res.items() if k != "stocks"}, default=str))


@fno_app.command("participants")
def fno_participants(
    start: str = typer.Option(
        ..., "--from", help="First day, YYYY-MM-DD (the archive has files from 2015 on)"
    ),
    end: str | None = typer.Option(None, "--to", help="Last day (default today, IST)"),
    dataset: str = typer.Option("both", help="oi, vol or both"),
    delay: float = typer.Option(
        2.0, min=0.0, help="Extra seconds between downloads, on top of the shared NSE limit"
    ),
    keep_raw: bool = typer.Option(True, help="Keep each CSV under data/fno_participant/ (~1 KB a file)"),
) -> None:
    """Download and store NSE's participant-wise F&O open interest and trading volume (Client/DII/FII/Pro) for
    [from, to] (#371). Resumable and idempotent: loaded days are skipped and failed days retried, so re-run the same
    command to continue. Personal use only (#362): stops by itself between 09:00 and 16:00 IST on weekdays, and stops
    a dataset after two refusals (403/429) or two failures in a row."""
    from datetime import date as _date

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    from finresearch.adapters import nse_participant_oi as po
    from finresearch.fnohist import participants

    kinds = {"oi": (po.OI,), "vol": (po.VOL,), "both": po.KINDS}.get(dataset)
    if kinds is None:
        raise typer.BadParameter("oi, vol or both", param_hint="--dataset")
    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(),
                  console=console) as bar:  # fmt: skip
        task = bar.add_task("participants", total=None)

        def progress(i: int, n: int, day, status: str) -> None:
            bar.update(task, total=n, completed=i, description=f"participants {day} {status}")

        res = asyncio.run(participants.backfill(_date.fromisoformat(start), _date.fromisoformat(end) if end else None,
                                                kinds=kinds, pause=delay, keep_raw=keep_raw, progress=progress))  # fmt: skip
    console.print_json(json.dumps(res, default=str))


@fno_app.command("derive")
def fno_derive(
    start: str = typer.Option(..., "--from", help="First day, YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--to", help="Last day (default today)"),
) -> None:
    """Recompute the derived IV rows and the contract calendar from stored rows (no network)."""
    from datetime import date as _date
    from datetime import timedelta

    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.fincalc.dates import today_ist
    from finresearch.fnohist import derive

    lo, hi = _date.fromisoformat(start), _date.fromisoformat(end) if end else today_ist()
    with session_scope() as s:
        days = s.scalars(select(FnoBhavDay.day).where(FnoBhavDay.status == "loaded", FnoBhavDay.day.between(lo, hi))
                         .order_by(FnoBhavDay.day)).all()  # fmt: skip
    n = 0
    for d in days:
        with session_scope() as s:
            n += derive.derive_day(s, d)
    with session_scope() as s:
        c = derive.rebuild_contracts(s, since=lo - timedelta(days=120))
    console.print(f"{len(days)} days, {n} IV rows, {c} contracts")


@fno_app.command("marks")
def fno_marks(
    start: str = typer.Option(..., "--from", help="First day, YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--to", help="Last day (default today)"),
) -> None:
    """Write the missing daily marks of open paper legs from stored bhavcopy days (#383; no network). Real Groww
    positions are marked only by the nightly monitor step (Groww keeps no position history)."""
    from datetime import date as _date

    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.fincalc.dates import today_ist
    from finresearch.fnobook import marks

    lo, hi = _date.fromisoformat(start), _date.fromisoformat(end) if end else today_ist()
    with session_scope() as s:
        days = s.scalars(select(FnoBhavDay.day).where(FnoBhavDay.status == "loaded", FnoBhavDay.day.between(lo, hi))
                         .order_by(FnoBhavDay.day)).all()  # fmt: skip
    n = u = 0
    for d in days:
        with session_scope() as s:
            got = marks.record(s, d)
        n, u = n + got["paper"], u + got["unpriced"]
    console.print(f"{len(days)} loaded days: {n} paper marks written ({u} unpriced)")


@fno_app.command("review")
def fno_review(
    month: str | None = typer.Option(None, "--month", help="YYYY-MM (default this month, IST)"),
) -> None:
    """The month's post-trade review in one line (#383): adherence, shortfall and the P&L explain. Reads only."""
    from finresearch.db import session_scope
    from finresearch.fincalc.dates import today_ist
    from finresearch.fnobook import review

    t = today_ist()
    if month:
        y, m = (int(x) for x in month.split("-"))
        t = t.replace(year=y, month=m, day=1)
    with session_scope() as s:
        console.print(
            review.brief_line(s, t) or f"F&O review ({t:%b %Y}): no paper trade or mark this month."
        )


backtest_app = typer.Typer(
    no_args_is_help=True,
    help="Backtest the pre-registered F&O strategies on the stored bhavcopy history (#307)",
)
fno_app.add_typer(backtest_app, name="backtest")


@backtest_app.command("run")
def fno_backtest_run(
    history_url: str | None = typer.Option(
        None,
        help="Database holding the F&O history, read only (e.g. postgresql+psycopg://localhost/finresearch_fnohist); "
        "default: the app's database",
    ),
    in_sample_only: bool = typer.Option(False, help="Replay 2022-01..2024-06 only (never reads a later day)"),
    json_out: Path | None = typer.Option(
        None, "--json", help="Also write the full report (every trade) here"
    ),
    store: bool = typer.Option(True, help="Save the report in the app's database (fno_backtest_run)"),
    fill: str = typer.Option(
        "same_print",
        help="same_print (registered) | next_open | next_close: a next-print sensitivity (#382), never a candidate",
    ),
) -> None:
    """Replay the pre-registered strategies (src/finresearch/backtest/specs.py) with real costs and slippage and judge
    them against the pass criteria stated before the out-of-sample run. Refuses out of sample if specs.py changed
    after its registration (backtest/prereg.py)."""
    from sqlalchemy.engine import make_url

    from finresearch.backtest import dbhistory, evaluate, specs
    from finresearch.backtest import store as bstore
    from finresearch.db import session_scope
    from finresearch.inventory import provenance

    url = history_url or get_settings().database_url
    h = dbhistory.DbHistory(history_url)
    stamp = provenance.code_stamp()  # #379: the engine code this run executes, hashed before it runs
    stock = dbhistory.top_stock(h, specs.IS_START, specs.IS_END)
    try:
        rep = evaluate.evaluate(h, stock=stock, source=make_url(url).database or "?", oos=not in_sample_only,
                                fill=fill)  # fmt: skip
    except evaluate.PreregistrationError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    with session_scope(
        history_url
    ) as s:  # #379: the bhavcopy manifest the replay read (store.save keeps both in detail)
        stamp |= provenance.data_hash(s, specs.IS_START, specs.IS_END if in_sample_only else None)
    rep = json.loads(json.dumps({**rep, **stamp}, default=str))
    if json_out:
        json_out.write_text(json.dumps(rep, indent=1))
        console.print(f"report: {json_out}")
    if store:
        with session_scope() as s:
            console.print(f"saved as run {bstore.save(s, rep)}")
    _backtest_table(rep)


@backtest_app.command("report")
def fno_backtest_report(
    json_in: Path | None = typer.Option(None, "--json", help="Read a report written by `run --json` instead"),
) -> None:
    """The latest saved evaluation: each strategy's status and out-of-sample evidence."""
    from finresearch.backtest import store as bstore
    from finresearch.db import session_scope

    if json_in:
        rep = json.loads(json_in.read_text())
    else:
        with session_scope() as s:
            run = bstore.latest_run(s)
            if run is None:
                console.print("no backtest saved yet: run `finresearch fno backtest run`")
                raise typer.Exit(1)
            rep = bstore.run_json(s, run)
    _backtest_table(rep)


@backtest_app.command("compare")
def fno_backtest_compare(
    same_json: Path = typer.Argument(..., help="A same-print report written by `run --json`"),
    next_json: Path = typer.Argument(
        ..., help="A next-print report (`run --fill next_open --json`) of the same data"
    ),
) -> None:
    """Same-print against next-print fills per spec (#382): paired ₹ difference per trade with its expiry-clustered
    one-sided t, the change in expectancy and in stop count, and the 'execution-sensitive' label (sign flip)."""
    from finresearch.backtest import compare

    same, nxt = json.loads(same_json.read_text()), json.loads(next_json.read_text())
    rows = compare.compare_reports(same, nxt)
    t = Table(title="Same-print vs next-print fills (D = same − next, ₹ per paired trade)")
    for c in ("strategy", "paired", "₹/trade same", "₹/trade next", "mean D", "t (D)", "p one-sided", "stops",
              "execution-sensitive"):  # fmt: skip
        t.add_column(c)
    for r in rows:
        if r.get("status") == "not compared":
            t.add_row(r["key"], "—", "—", "—", "—", "—", "—", "—", r["reason"][:60])
            continue
        flag = {True: "yes", False: "no", None: "unknown"}[r["execution_sensitive"]]
        t.add_row(r["key"], str(r["paired"]), _num(r["expectancy_same"], 0), _num(r["expectancy_next"], 0),
                  _num(r["mean_d"], 0), _num(r["t_d"]), _num(r["p_one_sided"], 3),
                  f"{r['stops_same']} → {r['stops_next']}", flag)  # fmt: skip
    console.print(t)
    pooled = compare.seller_tests(same, nxt)  # evals/experiments/execution_realism/PREREG.md H1 and H1b
    for name, key in (
        ("IV-filtered sellers pooled (H1)", "sellers"),
        ("top IV third (H1b)", "sellers_top_iv"),
    ):
        x = pooled[key]
        console.print(f"{name}: {x['paired']} pairs on {x['expiries']} expiries, mean D {_num(x['mean_d'], 0)}, "
                      f"t {_num(x['t_d'])}, one-sided p {_num(x['p_one_sided'], 3)}")  # fmt: skip


def _num(v: float | None, d: int = 2) -> str:
    return "—" if v is None else f"{v:,.{d}f}"


def _backtest_table(rep: dict) -> None:
    oos = rep.get("oos_evaluated")
    span = "out of sample " + " to ".join(rep["windows"]["oos"]) if oos else "in-sample only"
    t = Table(title=f"F&O backtest {rep['version']} on {rep['source']} · registration "
              f"{rep['registration_hash'][:12]} · {rep['variants_tried']} variants · {span}")  # fmt: skip
    for c in (
        "strategy",
        "status",
        "trades",
        "₹/trade",
        "t",
        "CI low",
        "max DD %",
        "DSR",
        "win %",
        "reasons",
    ):
        t.add_column(c)
    for r in rep["strategies"]:
        w = (r.get("windows") or {}).get("oos" if oos else "is") or {}
        win = None if w.get("win_rate") is None else w["win_rate"] * 100
        t.add_row(r["key"], r["status"], str(w.get("trades", "—")), _num(w.get("expectancy"), 0),
                  _num(w.get("t_stat")), _num(w.get("ci_low"), 0), _num(w.get("max_drawdown_pct"), 1),
                  _num(w.get("deflated_sharpe")), _num(win, 0), "; ".join(r.get("reasons", []))[:160])  # fmt: skip
    console.print(t)


direction_app = typer.Typer(
    no_args_is_help=True,
    help="The pre-registered F&O direction model: walk-forward evaluation, today's probabilities, ledger (#327)",
)
fno_app.add_typer(direction_app, name="direction")


@direction_app.command("run")
def fno_direction_run(
    history_url: str | None = typer.Option(
        None,
        help="Database holding the F&O history, read only (e.g. postgresql+psycopg://localhost/finresearch_fnohist2); "
        "default: the app's database",
    ),
    symbols: str = typer.Option(",".join(("NIFTY", "BANKNIFTY")), help="Comma-separated underlyings"),
    in_sample_only: bool = typer.Option(
        False, help="Evaluate up to 2024-06-30 only (never reads a later outcome)"
    ),
    json_out: Path | None = typer.Option(None, "--json", help="Also write the full report here"),
    store: bool = typer.Option(True, help="Save the report in the app's database (fno_direction_run)"),
) -> None:
    """Walk-forward evaluation of direction/specs.py: per underlying × horizon × event, Brier / log loss / AUC /
    calibration against the base rate and the market-implied model, Diebold-Mariano with Holm, and each cell's verdict.
    Refuses out of sample if specs.py changed after its registration (direction/prereg.py)."""
    from sqlalchemy.engine import make_url

    from finresearch.db import session_scope
    from finresearch.direction import data, evaluate, live

    url = history_url or get_settings().database_url
    panels, notes = {}, {}
    for sym in [x.strip().upper() for x in symbols.split(",") if x.strip()]:
        panels[sym], notes[sym] = data.load(sym, history_url)
    try:
        rep = evaluate.evaluate(panels, source=f"{make_url(url).database or '?'} (read only)", oos=not in_sample_only,
                                data_notes=notes)  # fmt: skip
    except evaluate.PreregistrationError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    rep = json.loads(json.dumps(rep, default=str))
    if json_out:
        json_out.write_text(json.dumps(rep, indent=1))
        console.print(f"report: {json_out}")
    if store:
        with session_scope() as s:
            console.print(f"saved as run {live.save_run(s, rep)}")
    _direction_table(rep)


@direction_app.command("report")
def fno_direction_report(
    json_in: Path | None = typer.Option(None, "--json", help="Read a report written by `run --json` instead"),
) -> None:
    """The latest saved evaluation, one row per underlying × horizon × event."""
    from finresearch.db import session_scope
    from finresearch.direction import live

    if json_in:
        rep = json.loads(json_in.read_text())
    else:
        with session_scope() as s:
            run = live.latest_run(s, matching=False)
            if run is None:
                console.print("no direction evaluation saved yet: run `finresearch fno direction run`")
                raise typer.Exit(1)
            rep = run.report
    _direction_table(rep)


@direction_app.command("log")
def fno_direction_log(
    symbols: str = typer.Option(",".join(("NIFTY", "BANKNIFTY")), help="Comma-separated underlyings"),
) -> None:
    """Log the latest stored session's forecasts to the ledger (the nightly job does this after the bhavcopy loads;
    refused once the next session has opened)."""
    from finresearch.direction import live

    for sym in [x.strip().upper() for x in symbols.split(",") if x.strip()]:
        res = live.log(sym, st=live.state(sym, fresh=True))
        console.print(
            f"{sym} {res['as_of']}: {len(res['logged'])} logged"
            + (f"; {res['skipped']}" if res["skipped"] else "")
        )


def _direction_table(rep: dict) -> None:
    span = f"OOS {rep['windows']['oos_start']} →" if rep.get("oos_evaluated") else "in-sample only"
    t = Table(title=f"F&O direction {rep['version']} on {rep['source']} · registration {rep['registration_hash'][:12]}"
              f" · {span}")  # fmt: skip
    for c in ("underlying", "h", "event", "n", "freq", "Brier impl", "Brier logit", "BSS vs impl", "p (Holm)",
              "BSS vs base", "p (Holm)", "hit", "AUC", "slope", "status"):  # fmt: skip
        t.add_column(c)
    for c in rep["cells"]:
        o = c.get("oos") if rep.get("oos_evaluated") else c.get("is")
        if not o or not o.get("n"):
            t.add_row(c["symbol"], str(c["horizon"]), c["event"], "0", *["—"] * 11)
            continue
        m, ts = o["models"], o["tests"]
        li, lb = ts["logistic_vs_implied"], ts["logistic_vs_base_rate"]
        t.add_row(c["symbol"], str(c["horizon"]), c["event"], str(o["n"]), _num(m["implied"]["base_rate"], 3),
                  _num(m["implied"]["brier"], 4), _num(m["logistic"]["brier"], 4), _num(li["brier_skill"], 4),
                  _num(li.get("p_holm", li["p"]), 3), _num(lb["brier_skill"], 4), _num(lb.get("p_holm", lb["p"]), 3),
                  _num(m["logistic"]["hit_rate"], 3), _num(m["logistic"]["auc"], 3),
                  _num(m["logistic"]["calibration_slope"], 2), c.get("status", "in-sample"))  # fmt: skip
    console.print(t)
    console.print(rep["verdict"])


lab_app = typer.Typer(no_args_is_help=True, help="F&O Strategy Lab background work (#318)")
fno_app.add_typer(lab_app, name="lab")


@lab_app.command("run-job")
def fno_lab_run_job(run_id: int) -> None:
    """Run one queued Strategy Lab backtest (the API starts this worker; it stops itself after the runtime cap)."""
    from finresearch.stratlab import jobs

    status = jobs.execute(run_id)
    console.print(f"lab run {run_id}: {status}")
    if status != "done":
        raise typer.Exit(1)


@lab_app.command("null-sim")
def fno_lab_null_sim(out: Path = typer.Option(..., "--json", help="Write every result here")) -> None:
    """#380's pre-registered null experiment (evals/experiments/lab_rigour/PREREG.md §5) on synthetic panels with the
    registered settings (stratlab.rigour). Pure computation: nothing is written to any database."""
    import json
    import time

    from finresearch.stratlab import nullsim, rigour

    registered = (rigour.NullSim(), rigour.Family()) == (rigour.NULL_SIM, rigour.FAMILY)
    if not registered:
        console.print("[yellow]NOT the registered settings: this run is not the pre-registered experiment[/]")
    t0 = time.time()
    res = nullsim.run(rigour.NULL_SIM, rigour.FAMILY, progress=lambda k, i, n: console.print(f"{k}: {i}/{n}")
                      if i == n or i % 20 == 0 else None)  # fmt: skip
    res["registered_settings"], res["seconds"] = registered, round(time.time() - t0, 1)
    out.write_text(json.dumps(res, indent=1) + "\n")
    for k, v in res["items"].items():
        console.print(f"{k}: {'pass' if v['pass'] else 'FAIL'} ({v['criterion']}) "
                      + ", ".join(f"{x}={v[x]:.4g}" for x in ("mean_pbo", "spa_reject_rate", "power")
                                  if isinstance(v.get(x), float)))  # fmt: skip


@lab_app.command("rigour")
def fno_lab_rigour(
    version_id: int | None = typer.Argument(None, help="also judge this version's promotion"),
) -> None:
    """The Lab's trial family (PBO, SPA, Romano-Wolf; #380) and, for a version, the forward-incubation gate. Reads only."""
    import json

    from finresearch.db import session_scope
    from finresearch.stratlab import family, promotion, store

    with session_scope() as s:
        fam = family.statistics(family.matrix(s))
        console.print(
            f"N = {store.n_trials(s)} trials; family: {fam['specs']} specs on {fam['days']} common sessions"
        )
        console.print(
            json.dumps({k: fam[k] for k in ("pbo", "spa", "romano_wolf", "note")}, indent=1, default=str)
        )
        if version_id is not None:
            got = promotion.judge(s, version_id, fam)
            for c in got["checks"]:
                console.print(
                    f"{'pass' if c['pass'] else 'FAIL'}  {c['criterion']}: {c['value']} ({c['rule']})"
                )
            console.print(f"status: {got['status']}")


@fno_app.command("status")
def fno_status() -> None:
    """Backfill coverage: days by status, rows stored, and the IV series per underlying."""
    from sqlalchemy import func, select

    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay, FnoIvEod

    with session_scope() as s:
        days = s.execute(select(FnoBhavDay.status, func.count(), func.min(FnoBhavDay.day), func.max(FnoBhavDay.day))
                         .group_by(FnoBhavDay.status)).all()  # fmt: skip
        rows = s.scalar(select(func.count()).select_from(FnoBhav))
        iv = s.execute(select(FnoIvEod.symbol, func.count(FnoIvEod.iv_30d), func.min(FnoIvEod.day),
                              func.max(FnoIvEod.day)).group_by(FnoIvEod.symbol).order_by(FnoIvEod.symbol)).all()  # fmt: skip
    t = Table(title=f"F&O bhavcopy: {rows:,} contract-days stored")
    for c in ("status", "days", "first", "last"):
        t.add_column(c)
    for r in days:
        t.add_row(*(str(x) for x in r))
    console.print(t)
    t = Table(title="Derived IV series (days with a 30-day IV)")
    for c in ("underlying", "days", "first", "last"):
        t.add_column(c)
    for r in iv:
        t.add_row(*(str(x) for x in r))
    console.print(t)


vault_app = typer.Typer(
    no_args_is_help=True,
    help="The sealed 2008-2021 F&O vault (#364, #365): load it into its own database; never read it here",
)
app.add_typer(vault_app, name="vault")


@vault_app.command("backfill")
def vault_backfill(
    start: str = typer.Option(..., "--from", help="First day, YYYY-MM-DD (from 2008-01-01)"),
    end: str = typer.Option(..., "--to", help="Last day, YYYY-MM-DD (to 2021-12-31)"),
    max_days: int | None = typer.Option(None, min=1, help="Request at most this many days in this run"),
    delay: float = typer.Option(
        2.5, min=0.0, help="Extra seconds between downloads, on top of the shared NSE limit"
    ),
    keep_raw: bool = typer.Option(
        False, help="Keep each zip under data/vault/fno_bhav/ (~1.3 GB in all; off)"
    ),
    probe_weekends: bool = typer.Option(
        False, help="Request every weekend day, not only the listed sessions"
    ),
) -> None:
    """Load NSE's legacy F&O bhavcopy for NIFTY/BANKNIFTY/FINNIFTY index futures and options into the vault database
    (FINRESEARCH_VAULT_DATABASE_URL), with derived IV rows and the contract calendar. Resumable: re-run the same command
    to continue. Personal use only (#362): no downloads 09:00-16:00 IST on weekdays; stops after two refusals
    (403/429) in a row. Prints counts only, never a vault figure."""
    from datetime import date as _date

    from finresearch.config import get_settings
    from finresearch.vault import backfill as vb

    def progress(i: int, n: int, day, status: str) -> None:
        if i % 25 == 0 or i == n or status == "failed":
            console.print(f"vault {i}/{n} {day} {status}")

    try:
        res = asyncio.run(vb.backfill(_date.fromisoformat(start), _date.fromisoformat(end),
                                      vault_url=get_settings().vault_database_url, pause=delay, keep_raw=keep_raw,
                                      max_days=max_days, probe_weekends=probe_weekends, progress=progress))  # fmt: skip
    except vb.VaultLoadRefused as e:
        console.print(f"[red]refused:[/red] {e}")
        raise typer.Exit(1) from e
    console.print_json(json.dumps(res, default=str))


@vault_app.command("status")
def vault_status() -> None:
    """Days tried in the vault database by year and status (the load manifest only; no market data is read)."""
    from finresearch.config import get_settings
    from finresearch.vault import backfill as vb

    url = get_settings().vault_database_url
    if not url:
        console.print("[red]no vault database is configured (FINRESEARCH_VAULT_DATABASE_URL)[/red]")
        raise typer.Exit(1)
    console.print_json(json.dumps(vb.status(url)))


@vault_app.command("engine-hash")
def vault_engine_hash() -> None:
    """The backtest engine's hash as PREREG.md §2.7 defines it, and the committed pin (vault/engine_pin.py)."""
    from finresearch.vault import engine_pin, registry

    h = registry.engine_hash()
    pin = engine_pin.ENGINE_HASH
    console.print(f"engine hash: {h}")
    console.print(
        "pin: "
        + ("no pin committed yet" if pin is None else pin + (" (matches)" if pin == h else " (DIFFERS)"))
    )


index_app = typer.Typer(
    no_args_is_help=True,
    help="Reference series: NSE's daily index close file (OHLC, P/E, P/B, yield, India VIX)",
)
app.add_typer(index_app, name="index")


@index_app.command("backfill")
def index_backfill(
    start: str = typer.Option(..., "--from", help="First day, YYYY-MM-DD (the archive starts in early 2013)"),
    end: str | None = typer.Option(None, "--to", help="Last day (default today, IST)"),
    delay: float = typer.Option(
        2.0, min=0.0, help="Extra seconds between downloads, on top of the shared NSE limit"
    ),
    keep_raw: bool = typer.Option(True, help="Keep each day's CSV under data/index_eod/ (~9 KB a day)"),
) -> None:
    """Download and store NSE's daily index close file (ind_close_all) for [from, to]. Resumable and idempotent:
    loaded days are skipped and failed days retried, so re-run the same command to continue. Personal use only
    (#362): stops by itself between 09:00 and 16:00 IST on weekdays and after two refusals (403/429) in a row."""
    from datetime import date as _date

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    from finresearch.refseries import index_eod as store

    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(),
                  console=console) as bar:  # fmt: skip
        task = bar.add_task("index close", total=None)

        def progress(i: int, n: int, day, status: str) -> None:
            bar.update(task, total=n, completed=i, description=f"index close {day} {status}")

        res = asyncio.run(store.backfill(_date.fromisoformat(start), _date.fromisoformat(end) if end else None,
                                         pause=delay, keep_raw=keep_raw, progress=progress))  # fmt: skip
    console.print_json(json.dumps(res, default=str))


equity_app = typer.Typer(
    no_args_is_help=True,
    help="Equity end of day: NSE's full bhavcopy with delivery (sec_bhavdata_full, #373)",
)
app.add_typer(equity_app, name="equity")


@equity_app.command("backfill")
def equity_backfill(
    start: str = typer.Option(
        ..., "--from", help="First day, YYYY-MM-DD (the file exists for 2020, per #373)"
    ),
    end: str | None = typer.Option(None, "--to", help="Last day (default today, IST)"),
    delay: float = typer.Option(
        2.0, min=0.0, help="Extra seconds between downloads, on top of the shared NSE limit"
    ),
    keep_raw: bool = typer.Option(True, help="Keep each day's CSV under data/equity_eod/ (~400 KB a day)"),
) -> None:
    """Download and store NSE's full equity bhavcopy (sec_bhavdata_full) for [from, to]: one file a day. Resumable
    and idempotent: loaded days are skipped and failed days retried, so re-run the same command to continue. Personal
    use only (#362): stops by itself between 09:00 and 16:00 IST on weekdays and after two refusals (403/429) in a
    row."""
    from datetime import date as _date

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    from finresearch.refseries import equity_eod as store

    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(),
                  console=console) as bar:  # fmt: skip
        task = bar.add_task("equity bhavcopy", total=None)

        def progress(i: int, n: int, day, status: str) -> None:
            bar.update(task, total=n, completed=i, description=f"equity bhavcopy {day} {status}")

        res = asyncio.run(store.backfill(_date.fromisoformat(start), _date.fromisoformat(end) if end else None,
                                         pause=delay, keep_raw=keep_raw, progress=progress))  # fmt: skip
    console.print_json(json.dumps(res, default=str))


@audit_app.command("equity-eod")
def audit_equity_eod(
    sample: int = typer.Option(
        50, min=1, max=500, help="Stock-days to compare (one NSE website request each)"
    ),
    seed: int = typer.Option(0, help="Draws the sample reproducibly; change it for another sample"),
    delay: float = typer.Option(1.0, min=0.0, help="Extra seconds between requests"),
) -> None:
    """#373 (a)'s acceptance check: random stored EQ closes from the full bhavcopy against NSE's price-history bar,
    exact. Makes `--sample` website requests (polite, shared NSE budget); refuses to run between 09:00 and 16:00 IST
    on weekdays and stops after two refusals. The nightly dq check (close_vs_quote) needs no requests."""
    from finresearch.refseries import equity_eod as store

    res = asyncio.run(store.reconcile_history(sample, seed=seed, pause=delay))
    console.print_json(json.dumps(res, default=str))
    if res["mismatches"]:
        raise typer.Exit(1)


rates_app = typer.Typer(
    no_args_is_help=True,
    help="Dated rate series from FBIL: G-sec par curve, T-bill, overnight and term MIBOR",
)
app.add_typer(rates_app, name="rates")


@rates_app.command("backfill")
def rates_backfill(
    start: str = typer.Option(
        ..., "--from", help="First day, YYYY-MM-DD (each series starts at FBIL's first day)"
    ),
    end: str | None = typer.Option(None, "--to", help="Last day (default today, IST)"),
    series: list[str] | None = typer.Option(
        None, "--series", help="gsec_par, tbill, mibor_on, mibor_term (repeatable; default all)"
    ),
    delay: float = typer.Option(
        2.0, min=0.0, help="Extra seconds between requests, on top of the per-host limit"
    ),
) -> None:
    """Download and store FBIL's dated rates for [from, to] (#366): the JSON fixings one month per request, the
    G-sec par curve one workbook (~0.73 MB, parsed and discarded) per published day. Resumable and idempotent: re-run
    the same command to continue. Needs the owner's data-rights decision for FBIL (finresearch.data_rights); stops
    between 09:00 and 16:00 IST on weekdays and after two refusals (403/429) in a row. Exit 1 when it stopped early."""
    from datetime import date as _date

    from finresearch import data_rights
    from finresearch.refseries import rates as store

    try:
        res = asyncio.run(store.backfill(series or list(store.SERIES), _date.fromisoformat(start),
                                         _date.fromisoformat(end) if end else None, pause=delay,
                                         progress=lambda key, status: console.print(f"{key} {status}")))  # fmt: skip
    except data_rights.NoDecision as e:
        console.print(str(e))
        raise typer.Exit(2) from e
    console.print_json(json.dumps(res, default=str))
    if res.get("stopped"):
        raise typer.Exit(1)


@audit_app.command("rates")
def audit_rates(start: str = typer.Option("2018-01-01", "--from", help="First day compared")) -> None:
    """#366's acceptance check on the stored FBIL rates (no network): the 3-month rate from the G-sec par curve vs
    FBIL's 3-month T-bill, both continuous, within 15 bp (the T-bill basis is an assumption, stated in the output)."""
    from datetime import date as _date

    from finresearch.db import session_scope
    from finresearch.refseries import checks

    with session_scope() as s:
        res = checks.par3m_vs_tbill(s, start=_date.fromisoformat(start))
    console.print_json(json.dumps(res, default=str))


@index_app.command("tri")
def index_tri(
    symbol: str = typer.Argument(..., help="F&O symbol (NIFTY, BANKNIFTY) or upper-cased index name"),
    start: str = typer.Option(..., "--from", help="First day (the series is based at its first close)"),
    end: str | None = typer.Option(None, "--to", help="Last day (default today, IST)"),
) -> None:
    """The APPROXIMATE total-return index from the stored closes and dividend yields (#366; NSE's official TRI is not
    fetched). Labelled approximate in the output; an unknown yield stops the chain."""
    from datetime import date as _date

    from finresearch.db import session_scope
    from finresearch.fincalc.dates import today_ist
    from finresearch.refseries.tri import tri_series

    with session_scope() as s:
        res = tri_series(
            s, symbol, _date.fromisoformat(start), _date.fromisoformat(end) if end else today_ist()
        )
    console.print_json(json.dumps(res, default=str))


@audit_app.command("index-eod")
def audit_index_eod(
    start: str = typer.Option("2024-01-01", "--from", help="First day of the underlying-vs-close check"),
    vix_from: str = typer.Option("2013-01-01", help="First day of the India VIX vs IV30 comparison"),
) -> None:
    """#366's acceptance checks on the stored data (no network): the F&O bhavcopy's underlying price vs the index
    close (2024 on: exact to 0.01, or every exception listed), and India VIX vs NIFTY's IV30 (change correlation,
    median and MAD of the level gap)."""
    from datetime import date as _date

    from finresearch.db import session_scope
    from finresearch.refseries import checks

    with session_scope() as s:
        res = {"underlying_vs_close": checks.underlying_vs_close(s, start=_date.fromisoformat(start)),
               "vix_vs_iv30": checks.vix_vs_iv30(s, start=_date.fromisoformat(vix_from))}  # fmt: skip
    console.print_json(json.dumps(res, default=str))


@fno_app.command("card-log")
def fno_card_log(
    since: str = typer.Option(
        ...,
        help="The split, e.g. a release's deploy time: ISO datetime WITH its zone, e.g. 2026-10-21T18:00:00+05:30",
    ),
) -> None:
    """Release report (#360): card verdicts (ENTER, NO PROVEN EDGE, WAIT, AVOID) before and after `--since`."""
    from datetime import datetime

    from finresearch.db import session_scope
    from finresearch.fnobook import card_log

    split = datetime.fromisoformat(since)
    if split.tzinfo is None:
        console.print("--since needs a time zone (e.g. +05:30): a naive time is ambiguous")
        raise typer.Exit(2)
    with session_scope() as s:
        got = card_log.verdict_counts(s, split)
    t = Table(title=f"F&O card verdicts before / after {split.isoformat()}")
    for c in ("verdict", "before", "after"):
        t.add_column(c)
    for k in dict.fromkeys([*got["before"], *got["after"]]):
        t.add_row(k, str(got["before"].get(k, 0)), str(got["after"].get(k, 0)))
    t.add_row("responses", str(got["responses"]["before"]), str(got["responses"]["after"]))
    console.print(t)


dq_app = typer.Typer(
    no_args_is_help=True, help="Data quality (#368): post-load checks, lineage and the replay"
)
app.add_typer(dq_app, name="dq")


@dq_app.command("run")
def dq_run() -> None:
    """Run every dataset's checks now and store the results (what the monitor does at 21:00 and 08:00 IST). Database
    only; prints each dataset's status."""
    from datetime import UTC, datetime

    from finresearch.db import session_scope
    from finresearch.dq import run

    with session_scope() as s:
        res = run.run_all(s, datetime.now(UTC))
    console.print_json(json.dumps(res, default=str))


@dq_app.command("replay")
def dq_replay(
    start: str = typer.Option(..., "--from", help="First day, YYYY-MM-DD"),
    end: str = typer.Option(..., "--to", help="Last day, YYYY-MM-DD"),
) -> None:
    """Acceptance 2's historical replay: the content checks over every loaded bhavcopy day in the range, read only.
    Prints counts per check, the flagged days and the share of sessions with a critical failure."""
    from datetime import date as _date

    from finresearch.db import session_scope
    from finresearch.dq import replay

    with session_scope() as s:
        res = replay.replay(s, _date.fromisoformat(start), _date.fromisoformat(end))
    console.print_json(json.dumps(res, default=str))


events_app = typer.Typer(no_args_is_help=True, help="The point-in-time event and macro calendar (#367)")
app.add_typer(events_app, name="events")


@events_app.command("seed")
def events_seed() -> None:
    """Load the hand-curated calendar (eventcal.seed: RBI MPC, Union Budgets, the 2024 election, FOMC) into the
    database; idempotent. Database only, no network."""
    from finresearch.db import session_scope
    from finresearch.eventcal import seed, store

    with session_scope() as s:
        ne, nc = store.upsert(s, seed.EVENTS, seed.COVERAGE)
    console.print(f"{ne} new events, {nc} new coverage rows ({len(seed.EVENTS)} events, {len(seed.COVERAGE)} "
                  "coverage rows in the seed)")  # fmt: skip


@events_app.command("check")
def events_check(
    start: str = typer.Option(..., "--from", help="Window start, YYYY-MM-DD (the plan day)"),
    end: str = typer.Option(..., "--to", help="Window end, YYYY-MM-DD (the expiry)"),
) -> None:
    """The plan card's tier-1 event check for a window, as known at 15:30 IST on its first day. Read only."""
    from datetime import date as _date

    from finresearch.db import session_scope
    from finresearch.eventcal import calendar as cal
    from finresearch.eventcal import store
    from finresearch.fincalc.dates import session_clock

    a, b = _date.fromisoformat(start), _date.fromisoformat(end)
    with session_scope() as s:
        ev, cov = store.load(s)
    console.print_json(
        json.dumps(cal.window_check(ev, cov, start=a, end=b, as_of=session_clock(a)), default=str)
    )


span_app = typer.Typer(no_args_is_help=True, help="NSE SPAN risk parameters (scan ranges) and FOVOLT")
fno_app.add_typer(span_app, name="span")


@span_app.command("backfill")
def fno_span_backfill(
    start: str = typer.Option(..., "--from", help="First day, YYYY-MM-DD (files exist from 2008)"),
    end: str | None = typer.Option(None, "--to", help="Last day (default today, IST)"),
    daily: bool = typer.Option(False, help="Every session instead of one file a month (~10 MB a request)"),
    delay: float = typer.Option(
        5.0, min=0.0, help="Extra seconds between downloads, on top of the shared NSE limit"
    ),
    keep_raw: bool = typer.Option(False, help="Keep the zips (the newest 30 only) under data/span/"),
) -> None:
    """Download NSE's SPAN files (and FOVOLT) for [from, to] and keep the per-underlying scan ranges, short-option
    minimum and spread tiers. One file a month by default (the first session from the 15th); each zip is parsed
    and discarded. Resumable: re-run the same command to continue. Personal use only (#362): stops by itself
    between 09:00 and 16:00 IST on weekdays and after two refusals (403/429) in a row."""
    from datetime import date as _date

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    from finresearch.fnohist import span

    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(),
                  console=console) as bar:  # fmt: skip
        task = bar.add_task("SPAN", total=None)

        def progress(i: int, n: int, day, status: str) -> None:
            bar.update(task, total=n, completed=i, description=f"SPAN {day} {status}")

        res = asyncio.run(span.backfill(_date.fromisoformat(start), _date.fromisoformat(end) if end else None,
                                        monthly=not daily, pause=delay, keep_raw=keep_raw, progress=progress))  # fmt: skip
    console.print_json(json.dumps(res, default=str))


@span_app.command("margin-check")
def fno_span_margin_check() -> None:
    """margin_approx against Groww's calculator on every logged live basket (MF16): the median and 95th percentile
    of |approx - Groww| / Groww, and whether they trigger the full margin engine (P4.3: median > 5 % or p95 > 15 %)."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import MarginCheck
    from finresearch.fincalc import margin_approx as ma

    with session_scope() as s:
        rows = [
            (float(a), float(g))
            for a, g in s.execute(select(MarginCheck.approx_margin, MarginCheck.groww_margin))
        ]
    console.print_json(json.dumps(ma.check_summary(rows)))


# keep last: `python -m finresearch.cli` (how the API spawns research and lab workers) must see every command above
if __name__ == "__main__":
    app()

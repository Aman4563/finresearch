"""FastAPI app: companies, documents, runs, steps, claims, reports, packs, limits and live run events.

Security: the server binds to 127.0.0.1 (see `finresearch serve`), accepts only localhost Host headers (DNS
rebinding), requires the local API token on every route but /api/health (finresearch.api.auth) and allows CORS
only from the local dashboard origins. Document and pack files are served only from
inside the configured data directories.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from finresearch import __version__
from finresearch.adapters.amfi import AmfiError
from finresearch.adapters.nse import NseError
from finresearch.adapters.sebi import SebiError
from finresearch.api.errors import public_error
from finresearch.api.workers import (
    Spawner,
    WorkerBusy,
    auto_resume_due,
    last_activity,
    stalled_info,
    worker_info,
)
from finresearch.config import get_settings
from finresearch.db import session_scope
from finresearch.db.models import (
    AgentStep,
    Alert,
    Claim,
    Company,
    Conversation,
    Decision,
    Document,
    MonitorJob,
    ResearchRun,
    SubscriptionSnapshotRow,
    Watch,
)

LOCAL_HOSTS = ["127.0.0.1", "localhost", "testserver"]
# the dashboard (3100) and local previews (3000-3009, e.g. a worktree build next to the live one)
DASHBOARD_ORIGINS = [
    f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (*range(3000, 3010), 3100)
]
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
EXPOSED_HEADERS = ["Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition", "ETag"]
CSRF_HEADER = "x-finresearch"  # the dashboard sends `X-FinResearch: 1` on every unsafe request
TERMINAL = ("done", "failed", "blocked")
RESEARCH_KINDS = ("ipo_report", "stock_report", "fund_report", "bond_report")
RADAR_TTL_S = 300
MCAP_SEARCH_SOURCE = "BSE scrip master: BSE's price × shares when the list was read (approximate; the stock page has the quote's)"
CITE_RE = re.compile(r"\[C(\d+)\]")


class StartRun(BaseModel):
    company: str
    kind: str = "ipo_report"
    streams: list[str] | None = None
    concurrency: int = Field(4, ge=1, le=8)


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question is empty")
        return v.strip()


class DecisionUpdate(BaseModel):
    user_action: Literal["applied", "skipped"] | None = None
    applied_lots: int | None = Field(None, ge=0)
    allotted_lots: int | None = Field(None, ge=0)
    issue_price: Decimal | None = Field(None, gt=0)
    listing_price: Decimal | None = Field(None, gt=0)
    exit_price: Decimal | None = Field(None, gt=0)
    exit_date: date | None = None
    notes: str | None = Field(None, max_length=4000)


class WatchBody(BaseModel):
    company: str
    kind: Literal["ipo", "stock"] = "ipo"


class StrategyLeg(BaseModel):
    right: Literal["call", "put", "future"]
    strike: Decimal = Field(gt=0, description="Strike (for a future: the entry price)")
    side: Literal["buy", "sell"]
    lots: int = Field(1, ge=1, le=100)
    premium: Decimal | None = Field(None, ge=0, description="Default: the chain's last price")


class Strategy(BaseModel):
    symbol: str
    expiry: date
    legs: list[StrategyLeg] = Field(min_length=1, max_length=8)
    rate: Decimal | None = Field(
        None,
        ge=-1,
        le=1,
        description="Risk-free rate for greeks (continuous, fraction); "
        "default: FBIL's G-sec par yield at the expiry (signals.rates)",
    )
    drift: Decimal | None = Field(None, ge=-1, le=1, description="Real-world annual drift; default = rate")
    charge_overrides: dict[str, Decimal] | None = Field(
        None, description="fincalc.charges keys -> rate (fraction)"
    )


class NewFund(BaseModel):
    scheme_code: str = Field(min_length=1, max_length=12)


class NewCompany(BaseModel):
    """A listed company by NSE symbol, or a BSE-only one by BSE scrip code (6 digits, or "BSE:<code>")."""

    nse_symbol: str | None = Field(None, min_length=1, max_length=30)
    bse_code: str | None = Field(None, min_length=6, max_length=10)
    name: str | None = None


class NewBond(BaseModel):
    isin: str = Field(min_length=12, max_length=12)


class ResumeRun(BaseModel):
    streams: list[str] | None = None
    concurrency: int | None = Field(
        None, ge=1, le=8, description="Default: the concurrency the run started with"
    )


# --------------------------------------------------------------------------- serialisers
def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def step_json(st: AgentStep) -> dict[str, Any]:
    return {"id": st.id, "key": st.key, "stage": st.stage, "role": st.role, "status": st.status,
            "attempts": st.attempts, "tier": st.tier, "model": st.model, "error": st.error,
            "num_turns": st.num_turns, "duration_s": st.duration_s, "cost_usd_est": st.cost_usd_est,
            "five_hour_before": st.five_hour_before, "five_hour_after": st.five_hour_after,
            "started_at": _iso(st.started_at), "finished_at": _iso(st.finished_at)}  # fmt: skip


def claim_json(c: Claim, docs: dict[int, str]) -> dict[str, Any]:
    """A ledger claim. `evidence` is its grade (verify.evidence, #242: A document-verified, B web quote checked on a
    stored page, C web unchecked, D fincalc-computed, U unsupported); each citation carries its own grade."""
    from finresearch.verify.evidence import citation_json, claim_grade

    ev = claim_grade(c)
    return {"id": c.id, "run_id": c.run_id, "stream": c.stream, "statement": c.statement,
            "evidence_grade": ev["grade"], "evidence_label": ev["label"],
            "claim_type": c.claim_type, "metric": c.metric,
            "value": str(c.value.normalize()) if c.value is not None else None, "unit": c.unit,
            "period": c.period, "importance": c.importance, "status": c.status, "verifier_note": c.verifier_note,
            "checks": c.checks or {}, "corrects_claim_id": c.corrects_claim_id,
            "citations": [{"document_id": x.document_id, "document_title": docs.get(x.document_id),
                           "page": x.page_no, "line_start": x.line_start, "line_end": x.line_end, "quote": x.quote,
                           "quote_found": x.quote_found, "url": x.url, "accessed_at": _iso(x.accessed_at),
                           **citation_json(x)}
                          for x in c.citations]}  # fmt: skip


def run_json(run: ResearchRun, co: Company | None, steps: dict[str, int] | None = None,
             last: Any = None) -> dict[str, Any]:  # fmt: skip
    """`last_error` / `pause_reason` say why a run failed or paused; `stalled` is set when the run is marked
    running but nothing is working on it (its worker exited), so the app can offer Resume."""
    m = run.manifest or {}
    worker = worker_info(m)
    err = m.get("last_error")
    return {"id": run.id, "kind": run.kind, "status": run.status, "company": co.slug if co else None,
            "company_name": co.name if co else None, "key": co.stock_key if co else None,
            "exchange": co.exchange if co else None, "created_at": _iso(run.created_at),
            "finished_at": _iso(run.finished_at), "resume_after": _iso(run.resume_after),
            "final_gate": m.get("final_gate"), "pack": m.get("pack"), "worker": worker,
            "last_error": err if err is None or isinstance(err, dict) else {"message": str(err)},
            "pause_reason": m.get("pause_reason") if run.status == "paused" else None,
            "pause_kind": m.get("pause_kind") if run.status == "paused" else None,
            "stalled": stalled_info(run, worker, last), "steps": steps or {}}  # fmt: skip


MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}
UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


def _download_name(title: str, suffix: str) -> str:
    """A readable file name from a document title: no path or control characters, one extension, at most 120 chars."""
    stem = re.sub(r"\s+", " ", UNSAFE_NAME.sub(" ", title)).strip(" .")[:120].strip(" .") or "document"
    return stem if stem.lower().endswith(suffix.lower()) else f"{stem}{suffix}"


def _content_disposition(kind: str, filename: str) -> str:
    """RFC 6266 header: a quoted ASCII `filename` for every browser plus RFC 5987 `filename*` when it isn't ASCII."""
    # whitespace collapsed with split/join and the gap before the extension removed with rpartition: no regex, so
    # linear on any input (CodeQL py/polynomial-redos flagged the earlier lookahead regex)
    ascii_name = " ".join(
        filename.encode("ascii", "ignore").decode().replace("\\", "_").replace('"', "'").split()
    )
    stem, dot, ext = ascii_name.rpartition(".")
    if dot:
        ascii_name = stem.rstrip() + dot + ext
    ascii_name = ascii_name.strip() or "file"
    if ascii_name == filename:
        return f'{kind}; filename="{filename}"'
    if ascii_name.startswith("."):  # the whole stem was non-ASCII
        ascii_name = f"file{ascii_name}"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _serve_file(path: Path, filename: str, *, download: bool, cache: str) -> FileResponse:
    """GET and HEAD, byte ranges (206), a readable name, inline unless `download`, and a known media type."""
    media = MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    headers = {
        "Content-Disposition": _content_disposition("attachment" if download else "inline", filename),
        "Cache-Control": cache,
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(path, media_type=media, headers=headers)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _latest_report(s, run_id: int) -> str | None:
    st = s.scalars(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.stage == "synthesis",
                                           AgentStep.status == "done")
                   .order_by(AgentStep.finished_at.desc().nulls_last(), AgentStep.id.desc())).first()  # fmt: skip
    return (st.output or {}).get("report_markdown") if st else None


# --------------------------------------------------------------------------- app
def create_app(*, spawner: Spawner | None = None, poll_s: float = 1.0, router=None, live_fetch=None,
               monitor: bool = False, monitor_deps=None, nse_detail=None, equity_list=None,
               nav_all=None, fno_client=None, bonds=None, clock=None, bse_scrips=None,
               api_token: str | None = None) -> FastAPI:  # fmt: skip
    """Test seams: `router` (bridge for chat and suggestions), `live_fetch` / `nse_detail` / `bonds` (NSE),
    `equity_list` / `bse_scrips` (the NSE equity list text and BSE's scrip-master rows, for stock search),
    `monitor_deps`, `clock` (() -> aware datetime, for the live routes' market hours).

    With monitor=True (as `finresearch serve` does) the monitoring scheduler runs inside the API process.

    `api_token`: the local API token every route but /api/health requires (finresearch.api.auth); by default the one
    `finresearch serve` bootstrapped (or FINRESEARCH_API_TOKEN). Without one the app refuses to start: it never
    serves personal data unauthenticated."""
    from finresearch.api.auth import ApiTokenGuard, current

    api_token = api_token or current()
    if not api_token:
        raise RuntimeError(
            "no local API token: start the API with `uv run finresearch serve` (it creates one)"
        )
    spawner = spawner or Spawner()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop, tasks = asyncio.Event(), []
        if monitor:
            from finresearch.monitor.scheduler import run_forever

            tasks.append(asyncio.create_task(run_forever(monitor_deps, stop=stop)))
            tasks.append(asyncio.create_task(_auto_resume_loop(stop)))
        yield
        stop.set()
        for task in tasks:
            await task

    async def _auto_resume_loop(stop: asyncio.Event, every_s: float = 60.0) -> None:
        """Restart runs paused by transient errors whose worker exited, once their resume time has passed."""
        while not stop.is_set():
            try:
                await asyncio.to_thread(auto_resume_due, spawner)
            except Exception:  # the database may be briefly unreachable; try again next minute
                logging.getLogger(__name__).exception("auto-resume check failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=every_s)

    app = FastAPI(title="FinResearch", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)  # fmt: skip
    # the last added middleware is outermost: CORS wraps the error and CSRF layers so their responses carry CORS
    # headers (a bare 500 without them looks like "API not reachable" in the dashboard)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
    app.add_middleware(CsrfGuard)
    app.add_middleware(CatchAll)
    # inside CORS, so a 401 carries CORS headers and the dashboard can tell "not authorised" from "not running"
    app.add_middleware(ApiTokenGuard, token=api_token)
    # credentials: the browser sends the app's token cookie with cross-origin calls from :3100 to the API
    # the in-app PDF viewer (pdf.js) reads the length/range headers to stream big documents in chunks
    app.add_middleware(CORSMiddleware, allow_origins=DASHBOARD_ORIGINS,
                       allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["*"],
                       allow_credentials=True, expose_headers=EXPOSED_HEADERS)  # fmt: skip

    @app.exception_handler(ValueError)
    async def _value_error(_req: Request, e: ValueError) -> JSONResponse:
        return JSONResponse({"detail": f"{type(e).__name__}: {e}"[:1000]}, status_code=422)

    @app.exception_handler(NseError)
    @app.exception_handler(SebiError)
    @app.exception_handler(AmfiError)
    @app.exception_handler(httpx.HTTPError)
    async def _upstream_error(_req: Request, e: Exception) -> JSONResponse:
        return JSONResponse(
            {"detail": f"upstream source failed: {type(e).__name__}: {e}"[:1000]}, status_code=502
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        with session_scope() as s:
            s.execute(select(1))
        return {"ok": True, "version": __version__}

    # ------------------------------------------------------------------ companies & documents
    @app.get("/api/companies")
    def companies() -> list[dict[str, Any]]:
        with session_scope() as s:
            docs = dict(
                s.execute(select(Document.company_id, func.count()).group_by(Document.company_id)).all()
            )
            runs = dict(s.execute(select(ResearchRun.company_id, func.max(ResearchRun.id))
                                  .where(ResearchRun.kind.in_(RESEARCH_KINDS)).group_by(ResearchRun.company_id)).all())  # fmt: skip
            kinds = dict(
                s.execute(
                    select(ResearchRun.id, ResearchRun.kind).where(ResearchRun.id.in_(runs.values()))
                ).all()
            )
            return [{"slug": c.slug, "name": c.name, "nse_symbol": c.nse_symbol, "bse_code": c.bse_code, "isin": c.isin,
                     "exchange": c.exchange, "key": c.stock_key, "documents": docs.get(c.id, 0),
                     "latest_run": runs.get(c.id), "kind": kinds.get(runs.get(c.id)) or company_kind(c)}
                    for c in s.scalars(select(Company).order_by(Company.name))]  # fmt: skip

    @app.get("/api/companies/{slug}")
    def company(slug: str) -> dict[str, Any]:
        with session_scope() as s:
            co = s.scalar(select(Company).where(Company.slug == slug))
            if co is None:
                raise HTTPException(404, f"unknown company {slug!r}")
            docs = s.scalars(select(Document).where(Document.company_id == co.id).order_by(Document.id)).all()
            runs = s.scalars(select(ResearchRun).where(ResearchRun.company_id == co.id)
                             .order_by(ResearchRun.id.desc())).all()  # fmt: skip
            return {"slug": co.slug, "name": co.name, "nse_symbol": co.nse_symbol, "bse_code": co.bse_code,
                    "isin": co.isin, "exchange": co.exchange, "key": co.stock_key,
                    "documents": [{"id": d.id, "kind": d.kind, "title": d.title, "pages": d.pages,
                                   "source_url": d.source_url, "fetched_at": _iso(d.fetched_at)} for d in docs],
                    "runs": [run_json(r, co) for r in runs]}  # fmt: skip

    @app.get("/api/documents/{doc_id}/lines")
    def document_lines(
        doc_id: int, start: int = Query(..., ge=1), end: int = Query(..., ge=1)
    ) -> dict[str, Any]:
        """Grep-compatible lines of a document's canonical text (for showing a citation in context)."""
        from finresearch.ingest.text import read_lines

        if end < start or end - start > 400:
            raise HTTPException(400, "end must be >= start and the range at most 400 lines")
        with session_scope() as s:
            d = s.get(Document, doc_id)
            if d is None or not d.text_path:
                raise HTTPException(404, "document or its text not found")
            text_path, title = Path(d.text_path), d.title
        if not text_path.exists():
            raise HTTPException(404, "document text file is missing")
        lines = read_lines(text_path)
        return {"document_id": doc_id, "title": title, "start": start,
                "lines": lines[start - 1 : min(end, len(lines))]}  # fmt: skip

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: int) -> dict[str, Any]:
        """A source document's metadata (the in-app viewer's title and page count)."""
        with session_scope() as s:
            d = s.get(Document, doc_id)
            if d is None:
                raise HTTPException(404, "unknown document")
            co = s.get(Company, d.company_id) if d.company_id else None
            return {"id": d.id, "kind": d.kind, "title": d.title, "pages": d.pages, "bytes": d.bytes,
                    "source_url": d.source_url, "fetched_at": _iso(d.fetched_at),
                    "filename": _download_name(d.title, Path(d.local_path).suffix or ".pdf"),
                    "company": {"slug": co.slug, "name": co.name} if co else None}  # fmt: skip

    @app.api_route("/api/documents/{doc_id}/file", methods=["GET", "HEAD"])
    def document_file(doc_id: int, download: bool = False) -> FileResponse:
        """The original file, inline (or as an attachment with ?download=1). Documents are sha256-addressed and never
        rewritten in place, so browsers may cache them for a long time."""
        with session_scope() as s:
            d = s.get(Document, doc_id)
            if d is None:
                raise HTTPException(404, "unknown document")
            path, title = Path(d.local_path), d.title
        if not (_within(path, get_settings().docs_dir) and path.is_file()):
            raise HTTPException(404, "document file not available")
        return _serve_file(path, _download_name(title, path.suffix or ".pdf"), download=download,
                           cache="private, max-age=604800, immutable")  # fmt: skip

    # ------------------------------------------------------------------ runs
    @app.get("/api/runs")
    def runs(company: str | None = None, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        with session_scope() as s:
            q = (select(ResearchRun, Company).join(Company, Company.id == ResearchRun.company_id, isouter=True)
                 .where(ResearchRun.kind.in_(RESEARCH_KINDS)))  # fmt: skip
            if company:
                q = q.where(Company.slug == company)
            rows = s.execute(q.order_by(ResearchRun.id.desc()).limit(limit)).all()
            counts: dict[int, dict[str, int]] = {}
            ids = [r.id for r, _ in rows]
            if ids:
                for rid, status, n in s.execute(select(AgentStep.run_id, AgentStep.status, func.count())
                                                .where(AgentStep.run_id.in_(ids))
                                                .group_by(AgentStep.run_id, AgentStep.status)):  # fmt: skip
                    counts.setdefault(rid, {})[status] = n
            reported = set(s.scalars(select(AgentStep.run_id).where(AgentStep.run_id.in_(ids), AgentStep.stage == "synthesis",
                                                                   AgentStep.status == "done"))) if ids else set()  # fmt: skip
            last = last_activity(s, ids)
            return [{**run_json(r, co, counts.get(r.id), last.get(r.id)), "has_report": r.id in reported}
                    for r, co in rows]  # fmt: skip

    @app.post("/api/runs", status_code=201)
    def start_run(body: StartRun) -> dict[str, Any]:
        from finresearch.orchestrator.ipo import create_run
        from finresearch.orchestrator.kinds import KINDS

        if body.kind not in KINDS:
            raise HTTPException(422, f"unknown research kind {body.kind!r}; choose from {sorted(KINDS)}")
        streams = KINDS[body.kind].default_streams
        if body.streams and (bad := set(body.streams) - set(streams)):
            raise HTTPException(422, f"unknown streams {sorted(bad)}; choose from {list(streams)}")
        with session_scope() as s:
            if s.scalar(select(Company.id).where(Company.slug == body.company)) is None:
                raise HTTPException(404, f"unknown company {body.company!r}; ingest its documents first")
        try:
            run_id = create_run(body.company, kind=body.kind)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        worker = spawner.start(run_id, streams=body.streams, concurrency=body.concurrency)
        return {"run_id": run_id, "worker": worker}

    @app.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: int, body: ResumeRun | None = None) -> dict[str, Any]:
        body = body or ResumeRun()
        try:
            worker = spawner.start(run_id, streams=body.streams, concurrency=body.concurrency)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except WorkerBusy as e:
            raise HTTPException(409, str(e)) from e
        return {"run_id": run_id, "worker": worker}

    def _run_detail(run_id: int) -> dict[str, Any]:
        with session_scope() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                raise HTTPException(404, f"unknown run {run_id}")
            co = s.get(Company, run.company_id) if run.company_id else None
            steps = s.scalars(
                select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.id)
            ).all()
            claims = dict(s.execute(select(Claim.status, func.count()).where(Claim.run_id == run_id)
                                    .group_by(Claim.status)).all())  # fmt: skip
            out = run_json(run, co, last=last_activity(s, [run_id]).get(run_id))
            out["steps"] = [step_json(x) for x in steps]
            out["claims"] = claims
            out["usage"] = {
                "five_hour_used": sum(max(0.0, x.five_hour_after - x.five_hour_before) for x in steps
                                      if x.five_hour_after is not None and x.five_hour_before is not None),
                "turns": sum(x.num_turns or 0 for x in steps),
                "minutes": sum(x.duration_s or 0 for x in steps) / 60,
            }  # fmt: skip
            return out

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int) -> dict[str, Any]:
        return _run_detail(run_id)

    @app.get("/api/runs/{run_id}/claims")
    def run_claims(run_id: int, status: str | None = None, stream: str | None = None,
                   cited: bool = False) -> list[dict[str, Any]]:  # fmt: skip
        with session_scope() as s:
            if s.get(ResearchRun, run_id) is None:
                raise HTTPException(404, f"unknown run {run_id}")
            q = select(Claim).where(Claim.run_id == run_id)
            if status:
                q = q.where(Claim.status == status)
            if stream:
                q = q.where(Claim.stream == stream)
            if cited:
                ids = {int(x) for x in CITE_RE.findall(_latest_report(s, run_id) or "")}
                q = q.where(Claim.id.in_(ids))
            rows = s.scalars(q.order_by(Claim.id)).all()
            docs = _doc_titles(s, rows)
            return [claim_json(c, docs) for c in rows]

    @app.get("/api/claims/{claim_id}")
    def claim(claim_id: int) -> dict[str, Any]:
        with session_scope() as s:
            c = s.get(Claim, claim_id)
            if c is None:
                raise HTTPException(404, f"unknown claim {claim_id}")
            out = claim_json(c, _doc_titles(s, [c]))
            out["corrected_by"] = list(s.scalars(select(Claim.id).where(Claim.corrects_claim_id == c.id)))
            return out

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: int) -> dict[str, Any]:
        """The latest synthesized report, the publish-gate verdict and every claim it cites."""
        from finresearch.verify.gate import check_report

        with session_scope() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                raise HTTPException(404, f"unknown run {run_id}")
            kind = run.kind
            md = _latest_report(s, run_id)
            if md is None:
                raise HTTPException(404, f"run {run_id} has no report yet")
            gate = check_report(s, run_id, md)
            s.rollback()  # the gate only reads; never persist anything from a GET
            ids = sorted({int(x) for x in CITE_RE.findall(md)})
            rows = (
                s.scalars(select(Claim).where(Claim.id.in_(ids), Claim.run_id == run_id)).all() if ids else []
            )
            docs = _doc_titles(s, rows)
            if (
                kind == "stock_report"
            ):  # the verdict box as a research view, also for reports written before #241
                from finresearch.signals.stock import neutral_report_markdown

                md = neutral_report_markdown(md)
            return {"run_id": run_id, "kind": kind, "markdown": md, "published": gate.ok,
                    "gate": {"ok": gate.ok, "blocking": gate.blocking, "warnings": gate.warnings},
                    "claims": {str(c.id): claim_json(c, docs) for c in rows}}  # fmt: skip

    @app.get("/api/runs/{run_id}/pack")
    def pack_files(run_id: int) -> dict[str, Any]:
        root = _pack_root(run_id)
        files = (
            sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []
        )
        entries = [{"path": f, "size": (root / f).stat().st_size} for f in files]
        return {"run_id": run_id, "path": str(root), "files": files, "entries": entries}

    @app.api_route("/api/runs/{run_id}/pack/{path:path}", methods=["GET", "HEAD"])
    def pack_file(run_id: int, path: str, download: bool = False) -> FileResponse:
        """One file of the run's pack, inline (or as an attachment with ?download=1), named after company and run."""
        root, slug, status = _pack_info(run_id)
        target = root / path
        if not (_within(target, root) and target.is_file()):
            raise HTTPException(404, "file not in this run's pack")
        # a finished run's pack only changes if the run is resumed: cache briefly; a running one not at all
        cache = "private, max-age=300" if status in TERMINAL else "no-cache"
        name = _download_name(f"{slug}-run-{run_id}-{target.stem}", target.suffix)
        return _serve_file(target, name, download=download, cache=cache)

    @app.get("/api/runs/{run_id}/insights")
    def run_insights(run_id: int) -> dict[str, Any]:
        """Chart-ready, cited figures for the report reader, derived from the ledger without any model."""
        from finresearch.api.insights import build_insights

        with session_scope() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                raise HTTPException(404, f"unknown run {run_id}")
            co = s.get(Company, run.company_id) if run.company_id else None
            st = s.scalars(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.stage == "synthesis",
                                                   AgentStep.status == "done")
                           .order_by(AgentStep.finished_at.desc().nulls_last(), AgentStep.id.desc())).first()  # fmt: skip
            synthesis = (st.output or {}) if st else {}
            rows = s.scalars(select(Claim).where(Claim.run_id == run_id).order_by(Claim.id)).all()
            claims = [claim_json(c, {}) for c in rows]
            meta = (co.meta or {}) if co else {}
            subject = {"slug": co.slug if co else None, "name": co.name if co else None,
                       "nse_symbol": co.nse_symbol if co else None, "isin": (co.isin if co else None) or meta.get("isin"),
                       "bse_code": co.bse_code if co else None, "key": co.stock_key if co else None,
                       "exchange": co.exchange if co else None, "amfi_code": meta.get("amfi_code")}  # fmt: skip
            watch = None
            w = s.scalars(select(Watch).where(Watch.company_id == co.id)).first() if co else None
            if w is not None and w.kind == "ipo":
                snaps = s.scalars(select(SubscriptionSnapshotRow)
                                  .where(SubscriptionSnapshotRow.nse_symbol == w.nse_symbol,
                                         SubscriptionSnapshotRow.source == "nse_combined")
                                  .order_by(SubscriptionSnapshotRow.as_of)).all()  # fmt: skip
                watch = {"id": w.id, **{k: _iso(getattr(w, k)) for k in ("open_date", "close_date", "allotment_date",
                                                                         "listing_date")},
                         "snapshots": [{"as_of": _iso(x.as_of), "source": x.source, "total_times": x.total_times,
                                        "categories": x.categories} for x in snaps]}  # fmt: skip
                subject["watch_id"] = w.id
            out = build_insights(run_id=run_id, kind=run.kind, claims=claims, synthesis=synthesis,
                                 report_markdown=synthesis.get("report_markdown"), subject=subject, watch=watch)  # fmt: skip
            s.rollback()
            return out

    def _pack_info(run_id: int) -> tuple[Path, str, str]:
        with session_scope() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                raise HTTPException(404, f"unknown run {run_id}")
            co = s.get(Company, run.company_id)
            slug = co.slug if co else "_"
            return get_settings().reports_dir / slug / f"run-{run_id}", slug, run.status

    def _pack_root(run_id: int) -> Path:
        return _pack_info(run_id)[0]

    # ------------------------------------------------------------------ live events
    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: int, request: Request) -> StreamingResponse:
        """Server-sent events: a snapshot, then one event per step or run change; ends when the run finishes."""
        await asyncio.to_thread(_run_detail, run_id)  # 404 before the stream starts
        return StreamingResponse(_events(run_id, request), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})  # fmt: skip

    async def _events(run_id: int, request: Request) -> AsyncIterator[str]:
        seq = 0

        def frame(event: str, data: Any) -> str:
            nonlocal seq
            seq += 1
            return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        detail = await asyncio.to_thread(_run_detail, run_id)
        yield frame("snapshot", detail)
        seen = {st["id"]: st for st in detail["steps"]}
        status = detail["status"]
        # without a live worker nothing will change the run (finished, crashed, killed, or never had one)
        while _worker_alive(detail):
            if await request.is_disconnected():
                return
            await asyncio.sleep(poll_s)
            detail = await asyncio.to_thread(_run_detail, run_id)
            for st in detail["steps"]:
                if seen.get(st["id"]) != st:
                    seen[st["id"]] = st
                    yield frame("step", st)
            if detail["status"] != status:
                status = detail["status"]
                yield frame("run", {k: detail[k] for k in ("id", "status", "resume_after", "final_gate", "last_error",
                                                           "pause_reason", "pause_kind", "stalled")})  # fmt: skip
            yield ": keep-alive\n\n"
        yield frame("end", {"id": run_id, "status": status})

    # ------------------------------------------------------------------ ask about a report
    @app.post("/api/runs/{run_id}/ask")
    async def ask_run(run_id: int, body: Ask) -> dict[str, Any]:
        from finresearch.agents.ask import ask
        from finresearch.bridge import AllTiersFailed

        try:
            return await ask(run_id, body.question, conversation_id=body.conversation_id, router=router)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except AllTiersFailed as e:
            raise HTTPException(503, f"Claude is not available right now: {e}"[:500]) from e

    @app.get("/api/runs/{run_id}/conversations")
    def conversations(run_id: int) -> list[dict[str, Any]]:
        with session_scope() as s:
            rows = s.scalars(select(Conversation).where(Conversation.run_id == run_id)
                             .order_by(Conversation.updated_at.desc())).all()  # fmt: skip
            return [{"id": c.id, "title": c.title, "updated_at": _iso(c.updated_at), "messages": len(c.messages)}
                    for c in rows]  # fmt: skip

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: int) -> dict[str, Any]:
        from finresearch.agents.ask import message_json

        with session_scope() as s:
            c = s.get(Conversation, conversation_id)
            if c is None:
                raise HTTPException(404, f"unknown conversation {conversation_id}")
            return {"id": c.id, "run_id": c.run_id, "title": c.title,
                    "messages": [message_json(m) for m in c.messages]}  # fmt: skip

    # ------------------------------------------------------------------ personal suggestions and journal
    @app.get("/api/profile")
    def get_profile() -> dict[str, Any]:
        from finresearch.suggest.advisor import load_profile

        with session_scope() as s:
            return load_profile(s).model_dump(mode="json")

    @app.put("/api/profile")
    def put_profile(body: dict[str, Any]) -> dict[str, Any]:
        from pydantic import ValidationError

        from finresearch.suggest.advisor import save_profile
        from finresearch.suggest.profile import Profile

        try:
            profile = Profile.model_validate(body)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
        ids = [r.id for r in profile.rules]
        if len(ids) != len(set(ids)):
            raise HTTPException(422, "rule ids must be unique")
        from finresearch.suggest.profile import rule_range_errors

        if errs := rule_range_errors(profile.rules):
            raise HTTPException(422, "; ".join(errs))
        alert_ids = [r.id for r in profile.alert_rules]
        if len(alert_ids) != len(set(alert_ids)):
            raise HTTPException(422, "alert rule ids must be unique")
        with session_scope() as s:
            return save_profile(s, profile).model_dump(mode="json")

    @app.get("/api/profile/stats")
    def profile_stats() -> dict[str, Any]:
        """Activity counts for the profile page: research runs, journal decisions and watches."""
        from finresearch.db.models import InvestorProfile
        from finresearch.suggest.advisor import PROFILE_NAME

        with session_scope() as s:

            def count(model: Any, *where: Any) -> int:
                return s.scalar(select(func.count()).select_from(model).where(*where)) or 0

            research = ResearchRun.kind.in_(RESEARCH_KINDS)
            first_run = s.scalar(select(func.min(ResearchRun.created_at)).where(research))
            updated = s.scalar(select(InvestorProfile.updated_at).where(InvestorProfile.name == PROFILE_NAME))
            return {
                "runs": count(ResearchRun, research),
                "runs_done": count(ResearchRun, research, ResearchRun.status == "done"),
                "decisions": count(Decision),
                "applied": count(Decision, Decision.user_action == "applied"),
                "watches": count(Watch),
                "active_watches": count(Watch, Watch.active),
                "first_run_at": _iso(first_run),
                "profile_updated_at": _iso(updated),
            }

    @app.post("/api/runs/{run_id}/suggest", status_code=201)
    async def suggest_run(run_id: int) -> dict[str, Any]:
        from finresearch.bridge import AllTiersFailed
        from finresearch.suggest.advisor import suggest

        with session_scope() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                raise HTTPException(404, f"unknown run {run_id}")
            if run.kind != "ipo_report":
                raise HTTPException(
                    422, f"suggestions are for IPO reports only; run {run_id} is a {run.kind}"
                )
        try:
            return await suggest(run_id, router=router, **({"fetch": live_fetch} if live_fetch else {}))
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:  # includes a suggestion that failed model validation
            raise HTTPException(422, f"{type(e).__name__}: {e}"[:1000]) from e
        except AllTiersFailed as e:
            raise HTTPException(503, f"Claude is not available right now: {e}"[:500]) from e

    @app.get("/api/decisions")
    def decisions(run_id: int | None = None) -> list[dict[str, Any]]:
        from finresearch.suggest.advisor import decision_json

        with session_scope() as s:
            q = select(Decision)
            if run_id is not None:
                q = q.where(Decision.run_id == run_id)
            rows = s.scalars(q.order_by(Decision.id.desc())).all()
            names = dict(s.execute(select(Company.id, Company.name)).all())
            return [{**decision_json(d), "company_name": names.get(d.company_id)} for d in rows]

    @app.patch("/api/decisions/{decision_id}")
    def update_decision(decision_id: int, body: DecisionUpdate) -> dict[str, Any]:
        from finresearch.suggest.advisor import decision_json, record_outcome

        with session_scope() as s:
            d = s.get(Decision, decision_id)
            if d is None:
                raise HTTPException(404, f"unknown decision {decision_id}")
            for k, v in body.model_dump(exclude_unset=True).items():
                setattr(d, k, v)
            d.outcome = record_outcome(d)
            s.flush()
            return decision_json(d)

    # ------------------------------------------------------------------ monitoring
    @app.get("/api/watches")
    def watches() -> list[dict[str, Any]]:
        from finresearch.monitor.watch import watch_json

        with session_scope() as s:
            out = []
            for w, co in s.execute(select(Watch, Company).join(Company, Company.id == Watch.company_id)
                                   .order_by(Watch.active.desc(), Watch.close_date.desc())):  # fmt: skip
                nxt = s.scalars(select(MonitorJob).where(MonitorJob.watch_id == w.id, MonitorJob.status == "pending")
                                .order_by(MonitorJob.due_at)).first()  # fmt: skip
                last = s.scalars(select(SubscriptionSnapshotRow).where(SubscriptionSnapshotRow.nse_symbol == w.nse_symbol)
                                 .order_by(SubscriptionSnapshotRow.as_of.desc())).first() if w.nse_symbol else None  # fmt: skip
                unread = s.scalar(select(func.count()).select_from(Alert).where(Alert.watch_id == w.id,
                                                                               Alert.read_at.is_(None)))  # fmt: skip
                out.append({**watch_json(w, co), "unread_alerts": unread,
                            "next_check": {"kind": nxt.kind, "due_at": _iso(nxt.due_at)} if nxt else None,
                            "last_subscription": {"as_of": _iso(last.as_of), "total_times": str(last.total_times)}
                            if last else None})  # fmt: skip
            return out

    @app.get("/api/monitor/schedule")
    def monitor_schedule() -> dict[str, Any]:
        """When each monitor check runs (IST), from the scheduler's constants and the profile's watch windows."""
        from finresearch.monitor.scheduler import schedule_json

        return schedule_json(running=monitor)

    @app.post("/api/watches", status_code=201)
    async def add_watch(body: WatchBody) -> dict[str, Any]:
        from finresearch.monitor.watch import watch_company, watch_stock

        try:
            if body.kind == "stock":
                return await asyncio.to_thread(watch_stock, body.company)
            return await watch_company(body.company, fetch_detail=nse_detail)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/api/watches/{watch_id}/stop")
    def stop_watch(watch_id: int) -> dict[str, Any]:
        from finresearch.monitor.watch import stop_watch

        out = stop_watch(watch_id)
        if out is None:
            raise HTTPException(404, f"unknown watch {watch_id}")
        return out

    @app.get("/api/watches/{watch_id}")
    def watch_detail(watch_id: int) -> dict[str, Any]:
        from finresearch.monitor.watch import watch_json

        with session_scope() as s:
            w = s.get(Watch, watch_id)
            if w is None:
                raise HTTPException(404, f"unknown watch {watch_id}")
            co = s.get(Company, w.company_id)
            jobs = s.scalars(
                select(MonitorJob).where(MonitorJob.watch_id == w.id).order_by(MonitorJob.due_at)
            ).all()
            snaps = s.scalars(select(SubscriptionSnapshotRow).where(SubscriptionSnapshotRow.nse_symbol == w.nse_symbol)
                              .order_by(SubscriptionSnapshotRow.as_of)).all() if w.nse_symbol else []  # fmt: skip
            alerts = s.scalars(select(Alert).where(Alert.watch_id == w.id).order_by(Alert.id.desc())).all()
            return {**watch_json(w, co),
                    "jobs": [{"id": j.id, "kind": j.kind, "slot": j.slot, "due_at": _iso(j.due_at), "status": j.status,
                              "attempts": j.attempts, "result": j.result, "error": j.error} for j in jobs],
                    "subscription": [{"as_of": _iso(x.as_of), "source": x.source, "total_times": str(x.total_times),
                                      "categories": x.categories} for x in snaps],
                    "alerts": [alert_json(a) for a in alerts]}  # fmt: skip

    @app.get("/api/alerts")
    def alerts(unread: bool = False, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
        with session_scope() as s:
            q = select(Alert, Watch).join(Watch, Watch.id == Alert.watch_id, isouter=True)
            if unread:
                q = q.where(Alert.read_at.is_(None))
            # `key` is the stock page key (NSE symbol or "BSE:<code>"), `label` how to show it ("BSE 526433")
            return [{**alert_json(a), "nse_symbol": w.nse_symbol if w else None, "exchange": w.exchange if w else None,
                     "key": w.key if w else None, "label": w.label if w else None}
                    for a, w in s.execute(q.order_by(Alert.id.desc()).limit(limit))]  # fmt: skip

    @app.post("/api/alerts/{alert_id}/read")
    def read_alert(alert_id: int) -> dict[str, Any]:
        with session_scope() as s:
            a = s.get(Alert, alert_id)
            if a is None:
                raise HTTPException(404, f"unknown alert {alert_id}")
            a.read_at = a.read_at or datetime.now(UTC)
            return alert_json(a)

    # ------------------------------------------------------------------ listed stocks
    equity_cache: dict[str, Any] = {}

    async def _equities() -> list:
        import time

        from finresearch.adapters.http import PoliteClient
        from finresearch.adapters.nse_equity import EQUITY_LIST_URL, parse_equity_list

        if equity_cache.get("at", 0) > time.time() - 86400:
            return equity_cache["rows"]
        if equity_list is not None:
            rows = parse_equity_list(await equity_list())
        else:
            async with PoliteClient() as c:
                resp = await c.get(EQUITY_LIST_URL, headers={"Referer": "https://www.nseindia.com/"})
            if not resp.ok:
                raise HTTPException(502, f"NSE equity list unavailable (HTTP {resp.status})")
            rows = parse_equity_list(resp.content.decode("utf-8", "replace"))
        equity_cache.update(at=time.time(), rows=rows)
        return rows

    bse_cache: dict[str, Any] = {}

    async def _bse_scrips() -> list:
        """BSE's active equity scrip master (a day on disk and in memory)."""
        import time

        from finresearch.adapters.bse_equity import BseEquity, parse_scrip_list

        if bse_cache.get("at", 0) > time.time() - 86400:
            return bse_cache["rows"]
        if bse_scrips is not None:
            rows = parse_scrip_list(await bse_scrips())
        else:
            async with BseEquity() as bse:
                rows = await bse.scrips()
        bse_cache.update(at=time.time(), rows=rows)
        return rows

    async def _listings(*, require_bse: bool = True):
        """NSE and BSE listings merged by ISIN. Without BSE's scrip master (`require_bse=False`) the NSE list alone
        comes back, and `bse_error` says why."""
        from finresearch.adapters.bse_equity import merge_listings

        nse_rows = await _equities()
        try:
            bse_rows, err = await _bse_scrips(), None
        except Exception as e:
            if require_bse:
                raise
            bse_rows, err = [], f"BSE scrip list unavailable: {type(e).__name__}: {e}"[:200]
        merged = merge_listings(nse_rows, bse_rows)
        if err is None:  # both lists: keep the ISIN map for the synchronous lookups (disclosures.store, #200)
            await asyncio.to_thread(_store_isin_map, merged)
        return merged, err

    def _store_isin_map(listings: Any) -> None:
        from finresearch.disclosures.store import record_isin_map

        try:
            with session_scope() as s:
                record_isin_map(s, listings, datetime.now(UTC))
        except Exception:  # a cache write never fails the listings read
            logging.getLogger(__name__).warning("could not store the ISIN map", exc_info=True)

    async def _listings_strict():
        return (await _listings())[0]

    app.state.listings = (
        _listings_strict  # markets / live routes resolve BSE keys and exchange switches with it
    )

    @app.get("/api/stocks/search")
    async def stock_search(q: str = Query(..., min_length=1, max_length=60)) -> list[dict[str, Any]]:
        """NSE and BSE equities by symbol, BSE scrip code or name. Each hit keeps the NSE list's fields (symbol,
        name, series, listed, isin) and adds `key` (the stock page / API key: the NSE symbol, or "BSE:<code>" for a
        BSE-only stock), `exchange` (NSE / BSE / both), and the BSE code, scrip id and group."""
        from finresearch.adapters.bse_equity import search_listings

        listings, bse_error = await _listings(require_bse=False)
        hits = search_listings(listings, q)
        known = await asyncio.to_thread(_known_symbols)
        fetched = bse_cache.get("at")
        mcap_as_of = datetime.fromtimestamp(fetched, UTC).isoformat() if fetched else None
        return [{**r.model_dump(mode="json", exclude={"market_cap_cr"}),
                 "market_cap_cr": float(r.market_cap_cr) if r.market_cap_cr is not None else None,
                 # BSE's scrip master figure (BSE's price x shares at the moment the list was read, kept up to a day):
                 # a size indication for ranking, not the quote's market cap, which the stock page computes
                 "market_cap_source": MCAP_SEARCH_SOURCE if r.market_cap_cr is not None else None,
                 "market_cap_as_of": mcap_as_of if r.market_cap_cr is not None else None,
                 "slug": known.get(r.key, {}).get("slug"),
                 "bse_error": bse_error} for r in hits]  # fmt: skip

    @app.post("/api/companies", status_code=201)
    async def add_company(body: NewCompany) -> dict[str, Any]:
        """Add a listed company by NSE symbol (name from NSE's equity list when not given), or a BSE-only stock by
        BSE scrip code (`bse_code` "526433" or "BSE:526433"; name and ISIN from BSE's scrip master). A BSE code whose
        ISIN also trades on NSE adds the NSE company (NSE stays the default exchange) with its BSE code noted."""
        from finresearch.adapters.bse_equity import scrip_code_of
        from finresearch.ingest.documents import get_or_create_company

        if (body.nse_symbol is None) == (body.bse_code is None):
            raise HTTPException(422, "give exactly one of nse_symbol or bse_code")
        bse_code = isin = None
        if body.bse_code is not None:
            raw = body.bse_code.strip().upper()
            code = scrip_code_of(raw) or (raw if raw.isdigit() and len(raw) == 6 else None)
            if code is None:
                raise HTTPException(422, f"{body.bse_code!r} is not a BSE scrip code (six digits)")
            try:
                listings = (await _listings())[0]
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(502, f"BSE scrip list unavailable: {type(e).__name__}: {e}"[:200]) from e
            row = listings.by_code(code)
            if row is None:
                raise HTTPException(404, f"BSE scrip {code} is not in BSE's list of active equities")
            if not row.nse_symbol:
                return await asyncio.to_thread(_add_bse_company, code, body.name or row.name, row.isin)
            sym, bse_code, isin = row.nse_symbol, code, row.isin  # dual-listed: the NSE company
            name, listed = body.name or row.name, True
        else:
            sym = (body.nse_symbol or "").strip().upper()
            name, listed = body.name, False
            if not name:
                match = next((e for e in await _equities() if e.symbol == sym), None)
                if match is None:
                    raise HTTPException(404, f"{sym} is not in NSE's list of listed equities")
                name, listed, isin = match.name, True, match.isin or None
        with session_scope() as s:
            existing = s.scalar(select(Company).where(Company.nse_symbol == sym))
            if existing is not None:
                existing.bse_code = existing.bse_code or bse_code
                existing.isin = existing.isin or isin
                return {"slug": existing.slug, "name": existing.name, "nse_symbol": sym, "created": False,
                        "kind": company_kind(existing), "exchange": "NSE", "key": sym}  # fmt: skip
            slug = _company_slug(name) or sym.lower()
            co = s.scalar(select(Company).where(Company.slug == slug))
            if co is not None and co.nse_symbol:  # the name's slug belongs to another symbol: never reuse it
                slug = f"{slug[: 79 - len(sym)]}-{sym.lower()}"
                co = s.scalar(select(Company).where(Company.slug == slug))
                if co is not None and co.nse_symbol:
                    raise HTTPException(409, f"slug {slug!r} already belongs to {co.nse_symbol}")
            created = co is None
            if co is None:
                co = get_or_create_company(s, slug, name, nse_symbol=sym)
            co.nse_symbol = sym  # a company added from documents alone has no symbol yet
            co.bse_code = co.bse_code or bse_code
            co.isin = co.isin or isin
            if listed and created:
                co.meta = {**(co.meta or {}), "kind": "stock_report"}
            return {
                "slug": co.slug,
                "name": co.name,
                "nse_symbol": sym,
                "created": created,
                "kind": company_kind(co),
                "exchange": "NSE",
                "key": sym,
            }

    # ------------------------------------------------------------------ mutual funds
    async def _scheme_rows() -> list:
        from finresearch.adapters.amfi import AmfiClient

        if nav_all is not None:
            return await nav_all()
        from finresearch.mcp_server.server import _nav_all

        async with AmfiClient() as amfi:
            return await _nav_all(amfi)

    @app.get("/api/funds/search")
    async def fund_search(q: str = Query(..., min_length=1, max_length=80)) -> list[dict[str, Any]]:
        from finresearch.adapters.amfi import search_schemes

        rows = search_schemes(await _scheme_rows(), q)
        with session_scope() as s:
            known = {slug for (slug,) in s.execute(select(Company.slug).where(Company.slug.like("mf-%")))}
        return [{"scheme_code": x.code, "name": x.name, "plan": x.plan, "option": x.option, "category": x.category,
                 "amc": x.amc, "nav": str(x.nav) if x.nav is not None else None, "nav_date": _iso(x.day),
                 "slug": f"mf-{x.code}" if f"mf-{x.code}" in known else None} for x in rows]  # fmt: skip

    @app.post("/api/funds", status_code=201)
    async def add_fund(body: NewFund) -> dict[str, Any]:
        from finresearch.orchestrator.fund import ensure_scheme_company

        try:
            return await ensure_scheme_company(body.scheme_code.strip(), nav_all=_scheme_rows)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e

    # ------------------------------------------------------------------ listed bonds
    bond_cache: dict[str, Any] = {}

    async def _bond_rows() -> list:
        import time

        from finresearch.adapters.nse_bonds import live_bonds

        if bond_cache.get("at", 0) > time.time() - RADAR_TTL_S:
            return bond_cache["rows"]
        rows = await (bonds() if bonds else live_bonds())
        bond_cache.update(at=time.time(), rows=rows)
        return rows

    @app.get("/api/bonds")
    async def bond_list(q: str = "", limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
        """NSE capital-market bonds matching a symbol or ISIN (no query: the most traded)."""
        from finresearch.adapters.nse_bonds import search_bonds

        rows = await _bond_rows()
        hits = (search_bonds(rows, q, limit) if q.strip() else
                sorted(rows, key=lambda b: -(b.traded_value or 0))[:limit])  # fmt: skip
        with session_scope() as s:
            known = {slug for (slug,) in s.execute(select(Company.slug).where(Company.slug.like("bond-%")))}
        return [{**b.model_dump(mode="json"), "warnings": b.warnings,
                 "slug": f"bond-{b.isin.lower()}" if f"bond-{b.isin.lower()}" in known else None} for b in hits]  # fmt: skip

    @app.post("/api/bonds", status_code=201)
    async def add_bond(body: NewBond) -> dict[str, Any]:
        from finresearch.orchestrator.bond import ensure_bond_company

        try:
            return await ensure_bond_company(body.isin.strip(), bonds=_bond_rows)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e

    # ------------------------------------------------------------------ F&O analytics (analysis only)
    def _fno():
        from finresearch.adapters.groww_fno import GrowwFirstFno

        # Groww's option chain when the Groww connection is valid, else NSE's (#267)
        return fno_client() if fno_client else GrowwFirstFno()

    @app.get("/api/fno/{symbol}/expiries")
    async def fno_expiries(symbol: str) -> dict[str, Any]:
        async with _fno() as f:
            expiries, strikes = await f.contract_info(symbol.upper())
        return {
            "symbol": symbol.upper(),
            "expiries": [e.isoformat() for e in expiries],
            "strikes": [str(x) for x in strikes],
        }

    @app.get("/api/fno/{symbol}/chain")
    async def fno_chain(symbol: str, expiry: date) -> dict[str, Any]:
        from finresearch.adapters.nse_fno import lot_size_for

        async with _fno() as f:
            chain = await f.option_chain(symbol.upper(), expiry)
            try:
                lot = lot_size_for(await f.lot_sizes(), symbol.upper(), expiry)
            except Exception:
                lot = None
        atm = chain.atm()
        return {"symbol": chain.symbol, "expiry": expiry.isoformat(), "underlying": str(chain.underlying),
                "as_of": _iso(chain.as_of), "lot_size": lot, "atm_strike": str(atm.strike) if atm else None,
                "atm_iv": {"call": str(atm.call.iv) if atm and atm.call else None,
                           "put": str(atm.put.iv) if atm and atm.put else None},
                "pcr_oi": str(chain.pcr()) if chain.pcr() is not None else None,
                "max_pain": str(chain.max_pain()) if chain.max_pain() is not None else None,
                "rows": [r.model_dump(mode="json") for r in chain.rows], "source_label": chain.source,
                "source": "https://groww.in/trade-api/docs/curl/live-data" if chain.source == "Groww"
                else "https://www.nseindia.com/option-chain"}  # fmt: skip

    @app.post("/api/fno/strategy")
    async def fno_strategy(body: Strategy) -> dict[str, Any]:
        """Payoff at expiry, breakevens, max profit/loss, probability of profit and net greeks (fincalc.options)."""
        from finresearch.adapters.nse_fno import lot_size_for
        from finresearch.fincalc import options as o
        from finresearch.fincalc.dates import now_ist

        sym = body.symbol.upper()
        async with _fno() as f:
            chain = await f.option_chain(sym, body.expiry)
            lot = lot_size_for(await f.lot_sizes(), sym, body.expiry)
        if not lot:
            raise HTTPException(422, f"no NSE lot size for {sym} {body.expiry:%b-%y}")
        spot = float(chain.underlying)
        t = max((body.expiry - now_ist().date()).days, 0.5) / 365
        from finresearch.signals.rates import risk_free, user_rate

        rinfo = user_rate(float(body.rate)) if body.rate is not None else await risk_free(t)
        rate = rinfo["rate"]
        by_strike = {r.strike: r for r in chain.rows}
        legs, greeks, notes = [], {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}, []
        for leg in body.legs:
            qty = leg.lots * lot * (1 if leg.side == "buy" else -1)
            if leg.right == "future":
                legs.append(o.Leg("future", float(leg.strike), 0.0, qty))
                greeks["delta"] += qty
                continue
            row = by_strike.get(leg.strike)
            quote = (row.call if leg.right == "call" else row.put) if row else None
            premium = (
                float(leg.premium)
                if leg.premium is not None
                else (float(quote.last_price) if quote and quote.last_price else None)
            )
            if premium is None:
                raise HTTPException(
                    422, f"no premium for {leg.right} {leg.strike}: pass one or pick a traded strike"
                )
            legs.append(o.Leg(leg.right, float(leg.strike), premium, qty))
            iv = (
                float(quote.iv) / 100
                if quote and quote.iv
                else o.implied_vol(leg.right, premium, spot, float(leg.strike), t, rate)
            )
            if not iv:
                notes.append(f"no implied volatility for {leg.right} {leg.strike}; its greeks are left out")
                continue
            g = o.greeks(leg.right, spot, float(leg.strike), t, rate, iv)
            for k in greeks:
                greeks[k] += getattr(g, k) * qty
        from finresearch.fincalc.charges import KEYS
        from finresearch.signals import fno as fs
        from finresearch.suggest.advisor import load_profile

        if body.charge_overrides and set(body.charge_overrides) - set(KEYS):
            raise HTTPException(422, f"unknown charge keys; use {list(KEYS)}")
        prof = o.profile(legs, spot)
        today = now_ist().date()
        async with _fno() as f:
            closes = await fs.underlying_closes(f, sym, today)
        with session_scope() as s:
            profile = load_profile(s)
        legs_in = [fs.LegIn(x.right, float(x.strike), x.side, x.lots, float(x.premium) if x.premium is not None else None)
                   for x in body.legs]  # fmt: skip
        try:
            a = fs.analyse(chain, lot, legs_in, today=today, closes=closes,
                           iv_series=[v for _, v, _ in fs.iv_series(sym)], capital=float(profile.fno_capital_inr),
                           max_loss_pct=float(profile.fno_max_loss_pct),
                           brokerage=float(profile.fno_brokerage_per_order_inr), rate=rate,
                           drift=None if body.drift is None else float(body.drift),
                           overrides=body.charge_overrides)  # fmt: skip
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        rn = a["risk_neutral"]
        return {"symbol": sym, "expiry": body.expiry.isoformat(), "spot": spot, "lot_size": lot, "as_of": _iso(chain.as_of),
                "breakevens": prof.breakevens, "max_profit": _money(prof.max_profit), "max_loss": _money(prof.max_loss),
                "net_premium": _money(prof.net_premium),
                # risk-neutral, after costs: a model probability under a lognormal price, not a forecast (#244)
                "model_probability_of_profit": None if rn is None else rn["pop"],
                "probability_of_profit": None if rn is None else rn["pop"],  # deprecated alias, one release
                "deprecated": {"probability_of_profit": "use model_probability_of_profit; removed in the next release"},
                "rate": rinfo,
                "net_greeks": {k: round(v, 4) for k, v in greeks.items()},
                "curve": [(round(x, 2), round(y, 2)) for x, y in prof.curve[:: max(1, len(prof.curve) // 120)]],
                "analysis": {k: v for k, v in a.items() if k not in ("legs", "breakevens", "max_profit", "net_premium")},
                "risk_notice": fs.risk_notice(),
                "notes": notes + a["notes"],
                "disclaimer": "Analysis only: model probabilities, not forecasts; costs from a dated table (some rates "
                              "unconfirmed); margin not computed. Not advice."}  # fmt: skip

    @app.get("/api/fno/risk-notice")
    def fno_risk_notice() -> dict[str, Any]:
        """SEBI's latest finding on individual F&O traders, with its source and date (roadmap §D.5)."""
        from finresearch.signals.fno import risk_notice

        return risk_notice()

    @app.get("/api/fno/{symbol}/iv")
    def fno_iv(symbol: str, days: int = Query(252, ge=1, le=1000)) -> dict[str, Any]:
        """Recorded daily ATM IV with IV rank / percentile (from 60 days) and the latest 25-delta skew."""
        from finresearch.fincalc.volatility import MIN_DAYS, WINDOW, iv_stats
        from finresearch.signals.fno import iv_series

        rows = iv_series(symbol)
        st = iv_stats([v for _, v, _ in rows])
        return {"symbol": symbol.upper(), "n": st.n, "min_days": MIN_DAYS, "window": WINDOW, "status": st.status,
                "current": st.current, "rank": st.rank, "percentile": st.percentile, "low": st.low, "high": st.high,
                "skew_25d": rows[-1][2] if rows else None, "last_day": rows[-1][0].isoformat() if rows else None,
                "series": [{"day": d.isoformat(), "atm_iv": v, "skew_25d": k} for d, v, k in rows[-days:]],
                "method": "ATM IV = mean of call and put IV at the strike nearest the underlying, nearest expiry at "
                          "least 7 days away, recorded after the close by the monitor. IVR = (IV − min)/(max − min) "
                          "and IVP = share of days below today, over the last 252 recorded days.",
                "source": "https://www.nseindia.com/option-chain"}  # fmt: skip

    # ------------------------------------------------------------------ IPO radar
    radar_cache: dict[str, Any] = {}

    @app.get("/api/ipos")
    async def ipos(refresh: bool = False) -> dict[str, Any]:
        """Current and upcoming NSE issues (mainboard and SME) plus BSE SME issues, linked to companies and runs
        already in the store. Each row carries its lot, minimum bid and per-category application amounts: NSE's
        issue page first, BSE's issue details as cross-check and fallback. A failed detail call only blanks that
        row's lot (noted in `notes`); `errors` is for list-level failures and keeps the response out of the cache."""
        import time

        from finresearch.adapters import bse
        from finresearch.adapters.nse import NseClient
        from finresearch.api.live import radar_ttl
        from finresearch.api.radar import terms_fields
        from finresearch.fincalc.dates import now_ist

        ttl = await asyncio.to_thread(radar_ttl, datetime.now(UTC), RADAR_TTL_S)
        if not refresh and radar_cache.get("at", 0) > time.time() - ttl:
            return radar_cache["data"]
        errors: list[str] = []
        notes: list[str] = []

        async def nse_side() -> tuple[list[tuple[str, Any]], dict[str, Any]]:
            issues: list[tuple[str, Any]] = []
            terms: dict[str, Any] = {}
            try:
                async with NseClient() as nse:
                    for phase, fetch in (("current", nse.current_issues), ("upcoming", nse.upcoming_issues)):
                        try:
                            issues += [(phase, i) for i in await fetch()]
                        except Exception as e:
                            errors.append(f"{phase}: {public_error(e, 180)}")
                    wanted = {i.symbol: i.series for _, i in issues}

                    async def one(sym: str, series: str | None) -> None:
                        try:
                            terms[sym] = await nse.issue_terms(sym, series)
                        except Exception as e:  # the row stays; BSE may still supply the lot
                            terms[sym] = f"NSE issue page: {type(e).__name__}"
                            notes.append(f"NSE {sym} issue page: {e}"[:200])

                    await asyncio.gather(*(one(s, ser) for s, ser in wanted.items()))
            except Exception as e:  # warm-up or connection failure
                errors.append(f"NSE: {public_error(e, 190)}")
            return issues, terms

        async def bse_side() -> list[tuple[Any, Any]]:
            try:
                rows, detail_errors = await bse.ipo_radar()
                notes.extend(f"bse: {e}" for e in detail_errors)
                return rows
            except Exception as e:
                errors.append(f"bse: {public_error(e, 190)}")
                return []

        (issues, nse_terms), bse_rows = await asyncio.gather(nse_side(), bse_side())
        bse_by_symbol = {d.symbol.upper(): d for _, d in bse_rows if d is not None and d.symbol}
        bse_by_name = {bse.name_key(i.company): d for i, d in bse_rows if d is not None}
        known = await asyncio.to_thread(_known_symbols)
        today = now_ist().date()
        rows, seen = [], set()
        for listed_as, i in issues:
            if i.symbol in seen:  # NSE's upcoming list repeats issues that are already open
                continue
            seen.add(i.symbol)
            phase = listed_as
            if i.issue_start and i.issue_end:
                phase = "upcoming" if today < i.issue_start else "closed" if today > i.issue_end else "open"
            k = known.get(i.symbol.upper(), {})
            t = nse_terms.get(i.symbol)
            other = bse_by_symbol.get(i.symbol.upper()) or bse_by_name.get(bse.name_key(i.company))
            row = {"phase": phase, "exchange": "NSE", "symbol": i.symbol, "company": i.company,
                   "series": i.series, "issue_start": _iso(i.issue_start), "issue_end": _iso(i.issue_end),
                   "price_band": i.price_band,
                   "times_subscribed": str(i.times_subscribed) if i.times_subscribed is not None else None,
                   "status": i.status, "slug": k.get("slug"), "latest_run": k.get("run"),
                   "latest_run_status": k.get("run_status")}  # fmt: skip
            row.update(terms_fields(series=i.series, list_band=i.price_band, bse=other,
                                    nse=t if not isinstance(t, str) else None,
                                    note=t if isinstance(t, str) else None))  # fmt: skip
            rows.append(row)
        on_nse = {bse.name_key(r["company"]) for r in rows} | {r["symbol"].upper() for r in rows}
        for i, d in bse_rows:
            if not i.is_sme_ipo:  # BSE mainboard issues are on NSE too; they only cross-check NSE's rows
                continue
            if bse.name_key(i.company) in on_nse or (d and d.symbol and d.symbol.upper() in on_nse):
                continue  # on both: NSE's row has the combined book
            phase = "upcoming" if i.issue_start and today < i.issue_start else "closed" if i.issue_end and \
                today > i.issue_end else "open"  # fmt: skip
            k = known.get(f"BSE:{i.ipo_no}", {})
            row = {"phase": phase, "exchange": "BSE", "symbol": (d.symbol if d else None) or i.scrip_code,
                   "company": i.company, "series": "SME", "issue_start": _iso(i.issue_start),
                   "issue_end": _iso(i.issue_end), "price_band": i.price_band, "times_subscribed": None,
                   "status": i.status, "bse_ipo_no": i.ipo_no,
                   "issue_size_shares": d.issue_size_shares if d else None, "slug": k.get("slug"),
                   "latest_run": k.get("run"), "latest_run_status": k.get("run_status")}  # fmt: skip
            row.update(terms_fields(series="SME", list_band=i.price_band, bse=d,
                                    note=None if d else "BSE issue details unavailable"))  # fmt: skip
            rows.append(row)
        order = {"open": 0, "upcoming": 1, "current": 1, "closed": 2}
        rows.sort(key=lambda r: (order.get(r["phase"], 3), r["issue_end"] or ""))
        data = {"fetched_at": _iso(datetime.now(UTC)), "issues": rows, "errors": errors, "notes": notes}
        if not errors:
            radar_cache.update(at=time.time(), data=data)
        return data

    @app.get("/api/ipo/base-rates")
    async def ipo_base_rates(by: str = Query("qib", pattern="^(qib|total)$")) -> dict[str, Any]:
        """How past mainboard IPOs opened on listing day, by final subscription band × regime (pre/post the Apr-2022
        NII reform): n, median and IQR of the open-vs-issue return, P(loss at open) with 95% Wilson intervals. Reads
        the harvested `ipo_history` table, or the committed snapshot while that is empty. Includes the listing
        model's walk-forward result (roadmap §D.1, items 2 and 14)."""
        from finresearch.evals.ipo_model import load_artefact, summary
        from finresearch.signals.ipo import DECISION_CAVEAT, base_rate_table

        table = await asyncio.to_thread(base_rate_table, by)
        art = load_artefact()
        model = None
        if art:
            model = {**summary(art), "uses_model": bool((art.get("gate") or {}).get("passes"))}
            from finresearch.evals.ipo_calibration import load_signal_artefact
            from finresearch.signals.ipo import calibrated_ready

            blend = load_signal_artefact()  # E-IPO-1 blend: a shadow test beside the table call (#147, #151)
            if not model["uses_model"] and calibrated_ready(blend):
                model["blend"] = {"lambda": blend["calibrator"]["params"]["lambda"], "gate": blend["gate"],
                                  "pooled": {k: v for k, v in blend["pooled"].items() if k != "reliability"},
                                  "source": blend.get("source"), "mode": "shadow"}  # fmt: skip
        return {
            **table,
            "event": "listing-day open vs the issue price",
            "caveat": DECISION_CAVEAT,
            "scope": "Mainboard (EQ) issues; final combined NSE+BSE book (activeCat), ex-anchor",
            "model": model,
        }

    # ------------------------------------------------------------------ plan usage
    @app.get("/api/limits")
    def limits() -> dict[str, Any]:
        from finresearch.bridge.limits import LimitTracker

        s = get_settings()
        return {"tiers": LimitTracker(s.state_dir).describe(), "now": datetime.now(UTC).timestamp(),
                "ceilings": {"five_hour": s.max_five_hour_ceiling}}  # fmt: skip

    @app.get("/api/usage/runs")
    def usage_by_run(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        """Plan usage per research run (newest first): the 5-hour window share, turns, agent minutes and steps."""
        with session_scope() as s:
            rows = s.execute(select(ResearchRun, Company).join(Company, Company.id == ResearchRun.company_id, isouter=True)
                             .where(ResearchRun.kind.in_(RESEARCH_KINDS))
                             .order_by(ResearchRun.id.desc()).limit(limit)).all()  # fmt: skip
            ids = [r.id for r, _ in rows]
            by_run: dict[int, dict[str, float]] = {}
            for st in s.scalars(select(AgentStep).where(AgentStep.run_id.in_(ids))) if ids else []:
                u = by_run.setdefault(st.run_id, {"five_hour_used": 0.0, "turns": 0, "minutes": 0.0, "steps": 0,
                                                  "cost_usd_est": 0.0})  # fmt: skip
                if st.five_hour_after is not None and st.five_hour_before is not None:
                    u["five_hour_used"] += max(0.0, st.five_hour_after - st.five_hour_before)
                u["turns"] += st.num_turns or 0
                u["minutes"] += (st.duration_s or 0) / 60
                u["steps"] += 1
                u["cost_usd_est"] += st.cost_usd_est or 0
            empty = {"five_hour_used": 0.0, "turns": 0, "minutes": 0.0, "steps": 0, "cost_usd_est": 0.0}
            return [{"run_id": r.id, "kind": r.kind, "status": r.status, "company_name": co.name if co else None,
                     "created_at": _iso(r.created_at), "finished_at": _iso(r.finished_at),
                     **{k: round(v, 4) for k, v in by_run.get(r.id, empty).items()}}
                    for r, co in rows]  # fmt: skip

    # ------------------------------------------------------------------ market data for the stock, fund and bond pages
    from finresearch.api.markets import add_market_routes

    add_market_routes(app, bond_rows=_bond_rows, scheme_rows=_scheme_rows)

    # ------------------------------------------------------------------ personal portfolio and capital-gains tax
    from finresearch.api.portfolio import add_portfolio_routes

    add_portfolio_routes(app, scheme_rows=_scheme_rows)
    from finresearch.api.portfolio_analytics import add_portfolio_analytics_routes

    add_portfolio_analytics_routes(app, scheme_rows=_scheme_rows)
    # fund look-through (AMC monthly portfolios)
    from finresearch.api.lookthrough import add_lookthrough_routes

    add_lookthrough_routes(app, scheme_rows=_scheme_rows)
    from finresearch.api.portfolio_health import add_portfolio_health_routes  # data-health panel (#219)

    add_portfolio_health_routes(app)

    # ------------------------------------------------------------------ live data while the market is open
    from finresearch.api.live import add_live_routes

    add_live_routes(app, monitor_deps=monitor_deps, clock=clock)

    from finresearch.api.freshness import add_freshness_routes

    add_freshness_routes(app, clock=clock)

    from finresearch.api.stock_signal import add_stock_signal_routes

    add_stock_signal_routes(app)

    # ------------------------------------------------------------------ intraday charts (NSE 1-minute series + archive)
    from finresearch.api.intraday import add_intraday_routes

    add_intraday_routes(app, clock=clock)

    # ------------------------------------------------------------------ alert rules for every asset + delivery
    from finresearch.api.alert_rules import add_alert_rule_routes

    add_alert_rule_routes(app, clock=clock)

    # ------------------------------------------------------------------ broker connections (read-only) + statement inbox
    from finresearch.api.connections import add_connection_routes

    add_connection_routes(app, clock=clock)

    # Groww market data in use + today's call budget (#267)
    from finresearch.api.market_data import add_market_data_routes

    add_market_data_routes(app)

    from finresearch.api.wealth import add_wealth_routes  # household finances (/wealth)

    add_wealth_routes(app, clock=clock)

    # ------------------------------------------------------------------ daily layer: brief, calendars, strip, export
    from finresearch.api.brief import add_brief_routes

    add_brief_routes(app, clock=clock)

    # ------------------------------------------------------------------ exchange disclosures (finresearch.disclosures)
    from finresearch.api.disclosures import add_disclosure_routes

    add_disclosure_routes(app, clock=clock)

    # ------------------------------------------------------------------ decision journal for every trade, behaviour report
    from finresearch.api.journal import add_journal_routes

    add_journal_routes(app, clock=clock)

    # ------------------------------------------------------------------ buy/sell signals (finresearch.signals)
    @app.get("/api/signals")
    def signal_assets() -> dict[str, Any]:
        from finresearch.signals import DISCLAIMER, providers
        from finresearch.signals.registry import ASSETS

        have = providers()
        return {"assets": [{"asset": a, "available": a in have} for a in ASSETS], "disclaimer": DISCLAIMER}

    @app.get("/api/signals/{asset}/{instrument}")
    async def signal(asset: str, instrument: str, request: Request) -> dict[str, Any]:
        """The signal for one instrument. Query parameters are passed to the provider as its context. Read-only
        (#247): it never writes the forecast ledger; POST to the same path logs the viewed signal."""
        return await _signal(asset, instrument, {**request.query_params, "log": "0"})

    @app.post("/api/signals/{asset}/{instrument}")
    async def signal_logged(asset: str, instrument: str, request: Request) -> dict[str, Any]:
        """The same signal, logged in the forecast ledger (signals.ledger: one open forecast per instrument, method
        and IST day) by the providers that log (stock, IPO). The app's signal views call this once per view."""
        return await _signal(asset, instrument, {**request.query_params, "log": "1"})

    async def _signal(asset: str, instrument: str, ctx: dict[str, Any]) -> dict[str, Any]:
        from finresearch.signals import get_provider

        provider = get_provider(asset)
        if provider is None:
            raise HTTPException(404, f"no signal provider for {asset!r} yet")
        try:
            sig = await provider(instrument, ctx)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        from finresearch.signals.reliability import reliability, scored_events

        out = sig.to_json()
        try:  # #218: base-rate n, validation and ledger calibration beside the probability
            with session_scope() as s:
                scored: int | None = scored_events(s, sig.asset, sig.method)
        except (
            Exception
        ):  # the ledger is unreadable: calibration is unknown, never "established", and the signal stands
            logging.getLogger(__name__).warning(
                "forecast ledger unreadable for the reliability line", exc_info=True
            )
            scored = None
        out["reliability"] = reliability(out, scored)
        return out

    # ------------------------------------------------------------------ forecast ledger and calibration
    @app.get("/api/forecasts")
    def forecasts(
        asset: str | None = None,
        instrument: str | None = None,
        status: Literal["open", "resolved", "void"] | None = None,
        method: str | None = None,
        run_id: int | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """Logged forecasts, newest first. Open ones are ordered by the date they resolve."""
        from finresearch.db.models import Forecast
        from finresearch.signals.ledger import forecast_json

        with session_scope() as s:
            q = select(Forecast)
            for col, val in ((Forecast.asset, asset), (Forecast.instrument, instrument), (Forecast.status, status),
                             (Forecast.method, method), (Forecast.run_id, run_id)):  # fmt: skip
                if val is not None:
                    q = q.where(col == val)
            total = s.scalar(select(func.count()).select_from(q.subquery()))
            order = ((Forecast.resolve_on, Forecast.id) if status == "open"
                     else (Forecast.created_at.desc(), Forecast.id.desc()))  # fmt: skip
            rows = s.scalars(q.order_by(*order).limit(limit).offset(offset)).all()
            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": [forecast_json(f) for f in rows],
            }

    @app.get("/api/calibration")
    def calibration(asset: str | None = None, bins: int = Query(5, ge=1, le=10)) -> dict[str, Any]:
        """Track record per asset and method: Brier score, skill vs the base rate, reliability bins and the hit rate
        with a Wilson 95 % interval, over resolved forecasts that carried a probability."""
        from finresearch.db.models import Forecast
        from finresearch.evals.calibration_policy import group_policy
        from finresearch.signals.ledger import CONFIDENCE_P, calibration_groups, forecast_json

        with session_scope() as s:
            groups = calibration_groups(s, asset, bins)
            for (
                g
            ) in groups:  # the recalibration tier the track record has reached (research §2.6; informational)
                g["policy"] = group_policy(g["asset"], g["n"])
            nxt = s.scalars(select(Forecast).where(Forecast.status == "open")
                            .order_by(Forecast.resolve_on, Forecast.id).limit(1)).first()  # fmt: skip
            scored = s.scalars(select(Forecast).where(Forecast.status == "open", Forecast.probability.isnot(None))
                               .order_by(Forecast.resolve_on, Forecast.id).limit(1)).first()  # fmt: skip
            return {"groups": groups, "confidence_map": CONFIDENCE_P,
                    "next_open": forecast_json(nxt) if nxt else None,
                    "next_scored": forecast_json(scored) if scored else None,
                    "min_n_for_recalibration": 50}  # fmt: skip

    return app


class CatchAll:
    """Unhandled errors become a JSON 500 inside the CORS layer (Starlette's own 500 is outside it)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def send_(msg: Message) -> None:
            nonlocal started
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        try:
            await self.app(scope, receive, send_)
        except Exception as e:
            if started:  # e.g. an event stream already under way: nothing left to answer with
                raise
            logging.getLogger("finresearch.api").exception("unhandled error on %s", scope.get("path"))
            resp = JSONResponse({"detail": f"server error: {public_error(e, 1000)}"}, status_code=500)
            await resp(scope, receive, send)


class CsrfGuard:
    """Any web page can make the browser send a body-less "simple" POST to 127.0.0.1. Unsafe methods therefore need
    the dashboard's origin (when the browser sends one) and the custom X-FinResearch header, which a cross-site page
    cannot add without a CORS preflight that only the dashboard origins pass. Clients without Origin (the CLI, curl,
    tests) are not browsers and are let through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            h = Headers(scope=scope)
            origin, site = h.get("origin"), h.get("sec-fetch-site")
            reason = None
            if origin is not None and origin not in DASHBOARD_ORIGINS:
                reason = f"origin {origin!r} is not the dashboard"
            elif (origin is not None or site == "cross-site") and h.get(CSRF_HEADER) != "1":
                reason = f"missing {CSRF_HEADER} header"
            if reason:
                return await JSONResponse({"detail": f"forbidden: {reason}"}, status_code=403)(
                    scope, receive, send
                )
        await self.app(scope, receive, send)


def _money(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def alert_json(a) -> dict[str, Any]:
    return {"id": a.id, "watch_id": a.watch_id, "kind": a.kind, "level": a.level, "message": a.message,
            "data": a.data or {}, "created_at": _iso(a.created_at), "read_at": _iso(a.read_at)}  # fmt: skip


def company_kind(co: Company) -> str:
    """The research kind a company is for: fund and bond slugs, listed stocks added from NSE's list, else an IPO."""
    if co.slug.startswith("mf-"):
        return "fund_report"
    if co.slug.startswith("bond-"):
        return "bond_report"
    return (co.meta or {}).get("kind") or "ipo_report"


def _company_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"\b(limited|ltd)\b", "", name.lower())).strip("-")[:70]


def _add_bse_company(code: str, name: str, isin: str | None) -> dict[str, Any]:
    """A BSE-only listed stock (no NSE listing for its ISIN), keyed by its scrip code. An existing company with the
    same code is returned; a company added from documents alone (same name slug, no listing yet) gets the code."""
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        existing = s.scalar(select(Company).where(Company.bse_code == code, Company.nse_symbol.is_(None)))
        if existing is not None:
            existing.isin = existing.isin or isin
            return {"slug": existing.slug, "name": existing.name, "nse_symbol": None, "bse_code": code,
                    "created": False, "kind": company_kind(existing), "exchange": "BSE", "key": f"BSE:{code}"}  # fmt: skip
        slug = _company_slug(name) or f"bse-{code}"
        co = s.scalar(select(Company).where(Company.slug == slug))
        if co is not None and (co.nse_symbol or co.bse_code):  # the slug is another listed company's
            slug = f"{slug[: 79 - len(code) - 4]}-bse-{code}"
            co = s.scalar(select(Company).where(Company.slug == slug))
            if co is not None and (co.nse_symbol or co.bse_code):
                raise HTTPException(409, f"slug {slug!r} already belongs to {co.stock_key}")
        created = co is None
        if co is None:
            co = get_or_create_company(s, slug, name, bse_code=code, isin=isin)
        co.bse_code, co.isin = code, co.isin or isin
        if created:
            co.meta = {**(co.meta or {}), "kind": "stock_report"}
        return {"slug": co.slug, "name": co.name, "nse_symbol": None, "bse_code": code, "created": created,
                "kind": company_kind(co), "exchange": "BSE", "key": f"BSE:{code}"}  # fmt: skip


def _known_symbols() -> dict[str, dict[str, Any]]:
    """Companies by NSE symbol, and BSE-only SME issues by "BSE:<IPO number>"."""
    with session_scope() as s:
        out: dict[str, dict[str, Any]] = {}
        for co in s.scalars(select(Company)):
            bse_no = (co.meta or {}).get("bse_ipo_no")
            if not co.nse_symbol and not bse_no and not co.bse_code:
                continue
            run = s.scalars(select(ResearchRun).where(ResearchRun.company_id == co.id, ResearchRun.kind == "ipo_report")
                            .order_by(ResearchRun.id.desc())).first()  # fmt: skip
            entry = {"slug": co.slug, "run": run.id if run else None, "run_status": run.status if run else None}  # fmt: skip
            if co.nse_symbol:
                out[co.nse_symbol.upper()] = entry
            elif co.bse_code:  # a BSE-only listed stock: "BSE:<6-digit scrip code>" (IPO numbers are shorter)
                out[f"BSE:{co.bse_code}"] = entry
            if bse_no:
                out[f"BSE:{bse_no}"] = entry
        return out


def _worker_alive(detail: dict[str, Any]) -> bool:
    return bool((detail.get("worker") or {}).get("alive"))


def _doc_titles(s, claims: list[Claim]) -> dict[int, str]:
    ids = {x.document_id for c in claims for x in c.citations if x.document_id}
    return (
        dict(s.execute(select(Document.id, Document.title).where(Document.id.in_(ids))).all()) if ids else {}
    )

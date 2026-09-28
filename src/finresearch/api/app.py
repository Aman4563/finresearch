"""FastAPI app: companies, documents, runs, steps, claims, reports, packs, limits and live run events.

Security: the server binds to 127.0.0.1 (see `finresearch serve`), accepts only localhost Host headers (DNS
rebinding) and allows CORS only from the local dashboard origins. Document and pack files are served only from
inside the configured data directories.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from finresearch.api.workers import Spawner, WorkerBusy, worker_info
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
DASHBOARD_ORIGINS = [f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (3000, 3100)]
TERMINAL = ("done", "failed", "blocked")
RESEARCH_KINDS = ("ipo_report", "stock_report")
RADAR_TTL_S = 300
CITE_RE = re.compile(r"\[C(\d+)\]")


class StartRun(BaseModel):
    company: str
    kind: str = "ipo_report"
    streams: list[str] | None = None
    concurrency: int = Field(4, ge=1, le=8)


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None


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


class NewCompany(BaseModel):
    nse_symbol: str = Field(min_length=1, max_length=30)
    name: str | None = None


class ResumeRun(BaseModel):
    streams: list[str] | None = None
    concurrency: int = Field(4, ge=1, le=8)


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
    return {"id": c.id, "run_id": c.run_id, "stream": c.stream, "statement": c.statement,
            "claim_type": c.claim_type, "metric": c.metric,
            "value": str(c.value.normalize()) if c.value is not None else None, "unit": c.unit,
            "period": c.period, "importance": c.importance, "status": c.status, "verifier_note": c.verifier_note,
            "checks": c.checks or {}, "corrects_claim_id": c.corrects_claim_id,
            "citations": [{"document_id": x.document_id, "document_title": docs.get(x.document_id),
                           "page": x.page_no, "line_start": x.line_start, "line_end": x.line_end, "quote": x.quote,
                           "quote_found": x.quote_found, "url": x.url, "accessed_at": _iso(x.accessed_at)}
                          for x in c.citations]}  # fmt: skip


def run_json(run: ResearchRun, co: Company | None, steps: dict[str, int] | None = None) -> dict[str, Any]:
    m = run.manifest or {}
    return {"id": run.id, "kind": run.kind, "status": run.status, "company": co.slug if co else None,
            "company_name": co.name if co else None, "created_at": _iso(run.created_at),
            "finished_at": _iso(run.finished_at), "resume_after": _iso(run.resume_after),
            "final_gate": m.get("final_gate"), "pack": m.get("pack"), "worker": worker_info(m),
            "steps": steps or {}}  # fmt: skip


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
               monitor: bool = False, monitor_deps=None, nse_detail=None, equity_list=None) -> FastAPI:  # fmt: skip
    """Test seams: `router` (bridge for chat and suggestions), `live_fetch` / `nse_detail` (NSE), `monitor_deps`.

    With monitor=True (as `finresearch serve` does) the monitoring scheduler runs inside the API process."""
    spawner = spawner or Spawner()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop, task = asyncio.Event(), None
        if monitor:
            from finresearch.monitor.scheduler import run_forever

            task = asyncio.create_task(run_forever(monitor_deps, stop=stop))
        yield
        stop.set()
        if task:
            await task

    app = FastAPI(title="FinResearch", version="0.3.0", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)  # fmt: skip
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
    app.add_middleware(CORSMiddleware, allow_origins=DASHBOARD_ORIGINS, allow_methods=["GET", "POST", "PUT", "PATCH"],
                       allow_headers=["*"])  # fmt: skip

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        with session_scope() as s:
            s.execute(select(1))
        return {"ok": True}

    # ------------------------------------------------------------------ companies & documents
    @app.get("/api/companies")
    def companies() -> list[dict[str, Any]]:
        with session_scope() as s:
            docs = dict(
                s.execute(select(Document.company_id, func.count()).group_by(Document.company_id)).all()
            )
            runs = dict(s.execute(select(ResearchRun.company_id, func.max(ResearchRun.id))
                                  .where(ResearchRun.kind.in_(RESEARCH_KINDS)).group_by(ResearchRun.company_id)).all())  # fmt: skip
            return [{"slug": c.slug, "name": c.name, "nse_symbol": c.nse_symbol, "documents": docs.get(c.id, 0),
                     "latest_run": runs.get(c.id)}
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
            return {"slug": co.slug, "name": co.name, "nse_symbol": co.nse_symbol,
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

    @app.get("/api/documents/{doc_id}/file")
    def document_file(doc_id: int) -> FileResponse:
        with session_scope() as s:
            d = s.get(Document, doc_id)
            if d is None:
                raise HTTPException(404, "unknown document")
            path = Path(d.local_path)
        if not (_within(path, get_settings().docs_dir) and path.exists()):
            raise HTTPException(404, "document file not available")
        return FileResponse(path, media_type="application/pdf")

    # ------------------------------------------------------------------ runs
    @app.get("/api/runs")
    def runs(company: str | None = None, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        with session_scope() as s:
            q = select(ResearchRun, Company).join(Company, Company.id == ResearchRun.company_id, isouter=True)
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
            return [run_json(r, co, counts.get(r.id)) for r, co in rows]

    @app.post("/api/runs", status_code=201)
    def start_run(body: StartRun) -> dict[str, Any]:
        from finresearch.orchestrator.ipo import create_run
        from finresearch.orchestrator.kinds import KINDS

        if body.kind not in KINDS:
            raise HTTPException(422, f"unknown research kind {body.kind!r}; choose from {sorted(KINDS)}")
        streams = KINDS[body.kind].default_streams
        if body.streams and (bad := set(body.streams) - set(streams)):
            raise HTTPException(422, f"unknown streams {sorted(bad)}; choose from {list(streams)}")
        try:
            run_id = create_run(body.company, kind=body.kind)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
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
            out = run_json(run, co)
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
            return {"run_id": run_id, "kind": kind, "markdown": md, "published": gate.ok,
                    "gate": {"ok": gate.ok, "blocking": gate.blocking, "warnings": gate.warnings},
                    "claims": {str(c.id): claim_json(c, docs) for c in rows}}  # fmt: skip

    @app.get("/api/runs/{run_id}/pack")
    def pack_files(run_id: int) -> dict[str, Any]:
        root = _pack_root(run_id)
        files = (
            sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []
        )
        return {"run_id": run_id, "path": str(root), "files": files}

    @app.get("/api/runs/{run_id}/pack/{path:path}")
    def pack_file(run_id: int, path: str) -> FileResponse:
        root = _pack_root(run_id)
        target = root / path
        if not (_within(target, root) and target.is_file()):
            raise HTTPException(404, "file not in this run's pack")
        return FileResponse(target)

    def _pack_root(run_id: int) -> Path:
        with session_scope() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                raise HTTPException(404, f"unknown run {run_id}")
            co = s.get(Company, run.company_id)
            return get_settings().reports_dir / (co.slug if co else "_") / f"run-{run_id}"

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
        while status not in TERMINAL or _worker_alive(detail):
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
                yield frame("run", {k: detail[k] for k in ("id", "status", "resume_after", "final_gate")})
            if detail.get("worker") and not _worker_alive(detail):
                break  # the app's worker exited (finished, crashed or paused without --wait)
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
        with session_scope() as s:
            return save_profile(s, profile).model_dump(mode="json")

    @app.post("/api/runs/{run_id}/suggest", status_code=201)
    async def suggest_run(run_id: int) -> dict[str, Any]:
        from finresearch.bridge import AllTiersFailed
        from finresearch.suggest.advisor import suggest

        try:
            return await suggest(run_id, router=router, **({"fetch": live_fetch} if live_fetch else {}))
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
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
                                 .order_by(SubscriptionSnapshotRow.as_of.desc())).first()  # fmt: skip
                unread = s.scalar(select(func.count()).select_from(Alert).where(Alert.watch_id == w.id,
                                                                               Alert.read_at.is_(None)))  # fmt: skip
                out.append({**watch_json(w, co), "unread_alerts": unread,
                            "next_check": {"kind": nxt.kind, "due_at": _iso(nxt.due_at)} if nxt else None,
                            "last_subscription": {"as_of": _iso(last.as_of), "total_times": str(last.total_times)}
                            if last else None})  # fmt: skip
            return out

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
        with session_scope() as s:
            w = s.get(Watch, watch_id)
            if w is None:
                raise HTTPException(404, f"unknown watch {watch_id}")
            w.active = False
            return {"id": w.id, "active": False}

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
                              .order_by(SubscriptionSnapshotRow.as_of)).all()  # fmt: skip
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
            q = select(Alert, Watch.nse_symbol).join(Watch, Watch.id == Alert.watch_id, isouter=True)
            if unread:
                q = q.where(Alert.read_at.is_(None))
            return [
                {**alert_json(a), "nse_symbol": sym}
                for a, sym in s.execute(q.order_by(Alert.id.desc()).limit(limit))
            ]

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

    @app.get("/api/stocks/search")
    async def stock_search(q: str = Query(..., min_length=1, max_length=60)) -> list[dict[str, Any]]:
        from finresearch.adapters.nse_equity import search_equities

        hits = search_equities(await _equities(), q)
        known = await asyncio.to_thread(_known_symbols)
        return [{**e.model_dump(mode="json"), "slug": known.get(e.symbol, {}).get("slug")} for e in hits]

    @app.post("/api/companies", status_code=201)
    async def add_company(body: NewCompany) -> dict[str, Any]:
        """Add a listed company by NSE symbol (name from NSE's equity list when not given)."""
        from finresearch.ingest.documents import get_or_create_company

        sym = body.nse_symbol.strip().upper()
        name = body.name
        if not name:
            match = next((e for e in await _equities() if e.symbol == sym), None)
            if match is None:
                raise HTTPException(404, f"{sym} is not in NSE's list of listed equities")
            name = match.name
        with session_scope() as s:
            existing = s.scalar(select(Company).where(Company.nse_symbol == sym))
            if existing is not None:
                return {"slug": existing.slug, "name": existing.name, "nse_symbol": sym, "created": False}
            slug = (
                re.sub(r"[^a-z0-9]+", "-", re.sub(r"\b(limited|ltd)\b", "", name.lower())).strip("-")[:70]
                or sym.lower()
            )
            co = get_or_create_company(s, slug, name, nse_symbol=sym)
            return {"slug": co.slug, "name": co.name, "nse_symbol": sym, "created": True}

    # ------------------------------------------------------------------ IPO radar
    radar_cache: dict[str, Any] = {}

    @app.get("/api/ipos")
    async def ipos(refresh: bool = False) -> dict[str, Any]:
        """Current and upcoming NSE mainboard issues, linked to companies and runs already in the store."""
        import time

        from finresearch.adapters.nse import NseClient
        from finresearch.fincalc.dates import now_ist

        if not refresh and radar_cache.get("at", 0) > time.time() - RADAR_TTL_S:
            return radar_cache["data"]
        errors: list[str] = []
        issues: list[tuple[str, Any]] = []
        async with NseClient() as nse:
            for phase, fetch in (("current", nse.current_issues), ("upcoming", nse.upcoming_issues)):
                try:
                    issues += [(phase, i) for i in await fetch()]
                except Exception as e:
                    errors.append(f"{phase}: {e}"[:200])
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
            rows.append({"phase": phase, "symbol": i.symbol, "company": i.company, "series": i.series,
                         "issue_start": _iso(i.issue_start), "issue_end": _iso(i.issue_end), "price_band": i.price_band,
                         "times_subscribed": str(i.times_subscribed) if i.times_subscribed is not None else None,
                         "status": i.status, "slug": k.get("slug"), "latest_run": k.get("run"),
                         "latest_run_status": k.get("run_status")})  # fmt: skip
        order = {"open": 0, "upcoming": 1, "current": 1, "closed": 2}
        rows.sort(key=lambda r: (order.get(r["phase"], 3), r["issue_end"] or ""))
        data = {"fetched_at": _iso(datetime.now(UTC)), "issues": rows, "errors": errors}
        if not errors:
            radar_cache.update(at=time.time(), data=data)
        return data

    # ------------------------------------------------------------------ plan usage
    @app.get("/api/limits")
    def limits() -> dict[str, Any]:
        from finresearch.bridge.limits import LimitTracker

        s = get_settings()
        return {"tiers": LimitTracker(s.state_dir).describe(), "now": datetime.now(UTC).timestamp(),
                "ceilings": {"five_hour": s.max_five_hour_ceiling}}  # fmt: skip

    return app


def alert_json(a) -> dict[str, Any]:
    return {"id": a.id, "watch_id": a.watch_id, "kind": a.kind, "level": a.level, "message": a.message,
            "data": a.data or {}, "created_at": _iso(a.created_at), "read_at": _iso(a.read_at)}  # fmt: skip


def _known_symbols() -> dict[str, dict[str, Any]]:
    with session_scope() as s:
        out: dict[str, dict[str, Any]] = {}
        for co in s.scalars(select(Company).where(Company.nse_symbol.isnot(None))):
            run = s.scalars(select(ResearchRun).where(ResearchRun.company_id == co.id, ResearchRun.kind == "ipo_report")
                            .order_by(ResearchRun.id.desc())).first()  # fmt: skip
            out[co.nse_symbol.upper()] = {"slug": co.slug, "run": run.id if run else None,
                                          "run_status": run.status if run else None}  # fmt: skip
        return out


def _worker_alive(detail: dict[str, Any]) -> bool:
    return bool((detail.get("worker") or {}).get("alive"))


def _doc_titles(s, claims: list[Claim]) -> dict[int, str]:
    ids = {x.document_id for c in claims for x in c.citations if x.document_id}
    return (
        dict(s.execute(select(Document.id, Document.title).where(Document.id.in_(ids))).all()) if ids else {}
    )

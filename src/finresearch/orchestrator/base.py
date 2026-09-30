"""A research pipeline: a deterministic, resumable DAG of research roles, shared by every research kind.

    facts ─► planner ─► streams ──(each, as soon as it finishes)──► verifier(stream)
                                                                    │ (barrier: all verified)
                                   bull ║ bear ─► synthesizer ─► critic ─┐
                                                      ▲                  │ high-severity gaps (≤ 2 rounds)
                                                      └── follow-up streams + verification ◄┘
                                                                    ─► report.md

A research kind (IPO report, stock report, ...) subclasses ResearchPipeline and sets its streams, the role that
fills each slot (planner, verifier, bull, bear, synthesizer, critic), the primary documents it needs, and its
deterministic facts. `finresearch.orchestrator.kinds` maps ResearchRun.kind to the pipeline class.

Guarantees
* Every step is an `agent_step` row keyed by (run_id, key). Finished steps are never re-run; `resume` continues a
  crashed or paused run from where it stopped.
* Budget-aware: before a Claude step starts, the Max 5-hour/7-day utilisation (last observed) plus the expected cost
  of the step and of steps already in flight must stay under the ceiling; otherwise the step is deferred and the run
  pauses until the reported window reset instead of failing midway.
* Research never silently degrades: roles run with allow_degraded=False, so a limit hit pauses the run.
* Transient failures (the Mac slept mid-response, two Claude Code processes refreshed the login at once, the network
  dropped) are retried in place with exponential backoff and jitter. When the retries run out the run pauses with
  the reason and a resume time instead of failing; `run_until_done(wait=True)` resumes it then.
* Nothing a step raises crashes the worker: the step is marked failed with the error, the run is marked failed (or
  paused) with a readable reason in the manifest (`last_error` / `pause_reason`), and `run()` returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select

from finresearch.agents.roles import ROLES
from finresearch.agents.runner import RoleOutputInvalid, RunContext, run_role
from finresearch.agents.schemas import CriticReport, ResearchPlan, StreamReport, VerificationReport
from finresearch.bridge import AllTiersFailed, BridgeRouter, Tier, build_router
from finresearch.bridge.limits import LimitTracker
from finresearch.bridge.types import AgentResult, SchemaViolation, TaskFailed, TransientError, transient_kind
from finresearch.config import get_settings
from finresearch.db import session_scope
from finresearch.db.models import AgentStep, Claim, Company, ResearchRun

logger = logging.getLogger(__name__)
RoleRunner = Callable[..., Awaitable[tuple[BaseModel, AgentResult]]]
DEFAULT_COST = {"stream": 0.08, "verify": 0.04, "plan": 0.03, "case": 0.03, "synthesis": 0.06, "critic": 0.03}


class RunPaused(Exception):
    """kind: "budget" (pre-flight ceiling), "limit" (plan limit hit) or "transient" (retries ran out)."""

    def __init__(self, reason: str, resume_after: datetime | None, kind: str = "limit"):
        super().__init__(reason)
        self.resume_after = resume_after
        self.kind = kind


TRANSIENT_LABELS = {
    "sleep": "the Mac went to sleep mid-response",
    "oauth": "another Claude Code process was refreshing the login",
    "network": "the connection to Claude dropped",
    "overloaded": "Claude was overloaded",
    "crash": "the Claude CLI exited without a result",
    "idle": "the Claude CLI stopped responding",
    "other": "a temporary Claude error",
}


class StepFailed(Exception):
    pass


@dataclass
class PipelineConfig:
    concurrency: int | None = None  # default: the run's saved choice, else 4
    max_followup_rounds: int = 2
    max_revisions: int = 2
    render: bool = True  # build the folder pack (md/html/pdf/xlsx/charts) when the run finishes
    discover: bool = True  # find and ingest documents first when the kind's primary documents are missing
    five_hour_ceiling: float = 0.92
    # transient failures: in-place retries per step, backoff (base * 2^n, capped, with jitter), then a pause whose
    # length doubles with each consecutive transient pause; after max_transient_pauses in a row the run fails
    transient_retries: int = 3
    retry_base_s: float = 30.0
    retry_cap_s: float = 480.0
    transient_pause_s: float = 600.0
    max_transient_pauses: int = 6
    streams: tuple[str, ...] | None = None  # default: the run's saved choice, else the kind's streams
    verify_importance: tuple[str, ...] = ("high", "normal")
    cost_defaults: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_COST))


def _now() -> datetime:
    return datetime.now(UTC)


class ResearchPipeline:
    kind = "research"
    version = "research-pipeline-1"
    default_streams: tuple[str, ...] = ()
    # role that fills each slot of the DAG
    roles: dict[str, str] = {"planner": "planner", "verifier": "verifier", "bull": "bull", "bear": "bear",  # noqa: RUF012
                             "synthesizer": "synthesizer", "critic": "critic"}  # fmt: skip
    # at least one of these must be in the store before research starts
    required_doc_kinds: tuple[str, ...] = ()
    discovery_kind = "ipo"  # how `ingest.discover` looks for this kind's documents
    subject = "a company"
    primary_source = "the company's filings"
    decision_deadline: str | None = None

    def __init__(self, run_id: int, *, runner: RoleRunner | None = None, router: BridgeRouter | None = None,
                 tracker: LimitTracker | None = None, config: PipelineConfig | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, rng: random.Random | None = None):  # fmt: skip
        self.run_id = run_id
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.config = config or PipelineConfig(five_hour_ceiling=get_settings().max_five_hour_ceiling)
        self._router = router
        self.tracker = tracker
        self.runner = runner or self._default_runner
        self.clock = clock
        # an explicit choice wins; otherwise run() restores the one saved at the start of the run
        self._chosen = (self.config.streams, self.config.concurrency)
        if self.config.streams is None:
            self.config.streams = self.default_streams
        if self.config.concurrency is None:
            self.config.concurrency = 4
        self._sem = asyncio.Semaphore(self.config.concurrency)
        self._reserved = 0.0
        self._paused: RunPaused | None = None
        self._transient_pauses = (
            0  # consecutive transient pauses without a finished step (kept in the manifest)
        )
        self.ctx: RunContext | None = None

    # ------------------------------------------------------------------ plumbing
    async def _default_runner(self, role: str, ctx: RunContext, **extra: str):
        return await run_role(role, ctx, router=self._get_router(), **extra)

    def _get_router(self) -> BridgeRouter:
        if self._router is None:
            self._router = build_router()
        return self._router

    def _tracker(self) -> LimitTracker:
        if self.tracker is None:
            self.tracker = self._get_router().tracker
        return self.tracker

    def _load_context(self, require_offer_doc: bool = False) -> RunContext:
        from finresearch.mcp_server.server import list_documents

        with session_scope() as s:
            run = s.get(ResearchRun, self.run_id)
            if run is None:
                raise ValueError(f"unknown run {self.run_id}")
            co = s.get(Company, run.company_id) if run.company_id else None
            if co is None:
                raise ValueError(f"run {self.run_id} has no company")
            facts = dict(run.manifest.get("facts", {}))
            slug, name, sym = co.slug, co.name, co.nse_symbol
        docs = json.loads(list_documents(slug))
        if require_offer_doc and self.required_doc_kinds and not self._has_primary_docs(docs):
            raise StepFailed(f"no {'/'.join(self.required_doc_kinds)} found for {slug}; run "
                             f"`finresearch docs discover {slug}` or `docs add`")  # fmt: skip
        extra = {"decision_deadline": self.decision_deadline} if self.decision_deadline else {}
        return RunContext(run_id=self.run_id, company_slug=slug, company_name=name, nse_symbol=sym, documents=docs,
                          facts=facts, subject=self.subject, primary_source=self.primary_source, **extra)  # fmt: skip

    def _has_primary_docs(self, docs: list[dict[str, Any]]) -> bool:
        return any(d["kind"] in self.required_doc_kinds for d in docs)

    def _set_run(self, **fields: Any) -> None:
        with session_scope() as s:
            run = s.get(ResearchRun, self.run_id)
            for k, v in fields.items():
                setattr(run, k, v)

    def _update_manifest(self, **fields: Any) -> None:
        with session_scope() as s:
            run = s.get(ResearchRun, self.run_id)
            run.manifest = {**(run.manifest or {}), **fields}

    # ------------------------------------------------------------------ budget
    def _expected_cost(self, stage: str, role: str) -> float:
        with session_scope() as s:
            avg = s.scalar(
                select(func.avg(AgentStep.five_hour_after - AgentStep.five_hour_before)).where(
                    AgentStep.role == role, AgentStep.status == "done",
                    AgentStep.five_hour_after.is_not(None), AgentStep.five_hour_before.is_not(None),
                    AgentStep.five_hour_after >= AgentStep.five_hour_before,
                )
            )  # fmt: skip
        return float(avg) if avg is not None and avg > 0 else self.config.cost_defaults.get(stage, 0.05)

    def _budget_check(self, stage: str, role: str) -> tuple[bool, float, float | None, datetime | None]:
        """(ok, expected_cost, current_utilisation, resume_after)."""
        tr = self._tracker()
        ok, _why = tr.availability(Tier.CLAUDE_MAX)
        snap = tr.snapshot(Tier.CLAUDE_MAX)
        now = self.clock()
        util = None
        reset = None
        if snap and snap.five_hour_utilization is not None and (snap.five_hour_resets_at or 0) > now:
            util = snap.five_hour_utilization
            reset = datetime.fromtimestamp(snap.five_hour_resets_at, UTC)
        cost = self._expected_cost(stage, role)
        if not ok:
            state = tr.describe().get(Tier.CLAUDE_MAX.value, {})
            until = state.get("cooling_until") or (snap.five_hour_resets_at if snap else None)
            return False, cost, util, datetime.fromtimestamp(until, UTC) if until else reset
        if util is not None and util + self._reserved + cost > self.config.five_hour_ceiling:
            return False, cost, util, reset
        return True, cost, util, None

    # ------------------------------------------------------------------ one step
    async def step(self, key: str, stage: str, role: str, **extra: str) -> BaseModel:
        model = ROLES[role].output
        with session_scope() as s:
            st = s.scalar(select(AgentStep).where(AgentStep.run_id == self.run_id, AgentStep.key == key))
            if st is not None and st.status == "done":
                return model.model_validate(st.output)
            if st is None:
                st = AgentStep(run_id=self.run_id, key=key, stage=stage, role=role)
                s.add(st)
            elif stage == "stream":
                self._drop_attempt_claims(s, st)
        async with self._sem:
            if self._paused:
                self._mark(key, status="deferred", error=f"run paused: {self._paused}")
                raise self._paused
            ok, cost, util, resume_after = self._budget_check(stage, role)
            if not ok:
                pause = RunPaused(
                    f"Max budget: {key} deferred (5h util {util}, step cost ~{cost:.2f})",
                    resume_after,
                    "budget",
                )
                self._paused = pause
                self._mark(key, status="deferred", error=str(pause))
                raise pause
            self._reserved += cost
            try:
                parsed, res, t0 = await self._attempts(key, stage, role, util, extra)
            finally:
                self._reserved -= cost
            rl = res.rate_limit
            self._mark(key, status="done", output=parsed.model_dump(mode="json"), tier=res.tier.value,
                       model=res.model, cost_usd_est=res.cost_usd_estimate, input_tokens=res.input_tokens,
                       output_tokens=res.output_tokens, duration_s=round(time.monotonic() - t0, 2),
                       num_turns=res.num_turns, five_hour_after=rl.five_hour_utilization if rl else None,
                       transcript_path=str(res.transcript_path) if res.transcript_path else None,
                       finished_at=_now(), error=None)  # fmt: skip
            if self._transient_pauses:
                self._transient_pauses = 0
                self._update_manifest(transient_pauses=0, auto_resumes=0)
            return parsed

    async def _attempts(self, key: str, stage: str, role: str, util: float | None,
                        extra: dict[str, str]) -> tuple[BaseModel, AgentResult, float]:  # fmt: skip
        """Run one step, retrying transient failures in place; a pause or a failure is recorded on the step."""
        retry = 0
        while True:
            self._mark(key, status="running", started_at=_now(), five_hour_before=util, inc_attempt=True,
                       claim_floor=stage == "stream")  # fmt: skip
            t0 = time.monotonic()
            try:
                parsed, res = await self.runner(role, self.ctx, **extra)
                return parsed, res, t0
            except AllTiersFailed as e:
                if e.limited:
                    raise self._limit_pause(key, e) from e
                transient = e.transient
                err: Exception = transient or e
                if transient is None and not (
                    retry == 0 and any(isinstance(x, SchemaViolation) for x in e.errors)
                ):
                    self._mark(key, status="failed", error=str(e), finished_at=_now())
                    raise StepFailed(f"{key}: {e}") from e
                kind = transient.kind if transient else "schema"
            except (TransientError, TaskFailed) as e:
                # the router re-raises TaskFailed; a transient message inside one (an older CLI wording) still retries
                kind = e.kind if isinstance(e, TransientError) else transient_kind(str(e))
                if kind is None:
                    self._mark(key, status="failed", error=f"{type(e).__name__}: {e}", finished_at=_now())
                    raise StepFailed(f"{key}: {e}") from e
                err = e
            except RoleOutputInvalid as e:
                self._mark(key, status="failed", error=str(e), finished_at=_now())
                raise StepFailed(f"{key}: {e}") from e
            except Exception as e:  # anything unexpected fails this step, never the whole worker
                self._mark(key, status="failed", error=f"{type(e).__name__}: {e}", finished_at=_now())
                raise StepFailed(f"{key}: {type(e).__name__}: {e}") from e
            # ---- a transient failure: retry in place, or pause the run when the retries are used up
            max_retries = 1 if kind == "schema" else 0 if kind == "timeout" else self.config.transient_retries
            if kind == "timeout":  # a step that ran out of time would only burn the plan again
                self._mark(key, status="failed", error=str(err), finished_at=_now())
                raise StepFailed(f"{key}: {err}") from err
            if retry >= max_retries:
                raise self._transient_pause(key, kind, err) from err
            retry += 1
            delay = self._retry_delay(kind, retry)
            self._mark(key, error=f"attempt failed ({TRANSIENT_LABELS.get(kind, kind)}): {err}"[:1500]
                       + f" | retry {retry}/{max_retries} in {delay:.0f}s")  # fmt: skip
            await self.sleep(delay)
            if self._paused:  # another step paused the run meanwhile
                self._mark(key, status="deferred", error=f"run paused: {self._paused}")
                raise self._paused
            if stage == "stream":
                with session_scope() as s:
                    st = s.scalar(
                        select(AgentStep).where(AgentStep.run_id == self.run_id, AgentStep.key == key)
                    )
                    self._drop_attempt_claims(s, st)

    def _retry_delay(self, kind: str, retry: int) -> float:
        """Exponential backoff with jitter; a login-refresh collision waits 30-90 s for the other process."""
        if kind == "oauth":
            return self.rng.uniform(30, 90)
        base = min(self.config.retry_cap_s, self.config.retry_base_s * 2 ** (retry - 1))
        return base + self.rng.uniform(0, base / 2)

    def _limit_pause(self, key: str, e: AllTiersFailed) -> RunPaused:
        tr = self._tracker()
        state = tr.describe().get(Tier.CLAUDE_MAX.value, {})
        until = state.get("cooling_until")
        reason = state.get("reason") or ""
        circuit = "circuit open" in reason
        resume = datetime.fromtimestamp(until, UTC) if until and until > self.clock() else None
        if circuit:
            pause = RunPaused(f"Claude kept failing during {key} ({reason}); retrying when the cool-down ends",
                              resume or _now() + timedelta(seconds=self.config.transient_pause_s), "transient")  # fmt: skip
        else:
            pause = RunPaused(f"Claude limit during {key}", resume)
        self._paused = pause
        self._mark(key, status="deferred", error=str(e))
        return pause

    def _transient_pause(self, key: str, kind: str, err: Exception) -> RunPaused:
        self._transient_pauses += 1
        n = self._transient_pauses
        self._update_manifest(transient_pauses=n)
        what = TRANSIENT_LABELS.get(kind, "the model's output did not match the schema")
        if n > self.config.max_transient_pauses:
            self._mark(key, status="failed", error=str(err), finished_at=_now())
            raise StepFailed(f"{key}: still failing after {n - 1} automatic pauses ({what}): {err}. Check that "
                             f"`claude` works in a terminal (sign in again if needed), then resume.")  # fmt: skip
        delay = min(self.config.transient_pause_s * 2 ** (n - 1), 4 * 3600)
        resume = _now() + timedelta(seconds=delay)
        tries = 1 if kind == "schema" else self.config.transient_retries
        pause = RunPaused(f"{key}: {what} (retried {tries} times); the run resumes automatically in about "
                          f"{delay / 60:.0f} min. Last error: {err}"[:2000], resume, "transient")  # fmt: skip
        self._paused = pause
        self._mark(key, status="deferred", error=str(pause))
        return pause

    def _mark(self, key: str, *, inc_attempt: bool = False, claim_floor: bool = False, **fields: Any) -> None:
        with session_scope() as s:
            st = s.scalar(select(AgentStep).where(AgentStep.run_id == self.run_id, AgentStep.key == key))
            for k, v in fields.items():
                setattr(st, k, v)
            if inc_attempt:
                st.attempts += 1
            if claim_floor and "claim_floor" not in (st.output or {}):
                # the ledger's high-water mark when the stream first started: its claims all lie above it
                top = s.scalar(select(func.max(Claim.id)).where(Claim.run_id == self.run_id))
                st.output = {**(st.output or {}), "claim_floor": top or 0}

    def _drop_attempt_claims(self, s, st: AgentStep) -> None:
        """A stream step that is run again (deferred, failed or crashed mid-attempt) starts from a clean ledger:
        the claims its earlier attempts saved are deleted (citations cascade), so the rerun does not duplicate them.
        Claims of earlier rounds of the same stream lie below the floor; verifier corrections are kept."""
        floor = (st.output or {}).get("claim_floor")
        if st.status == "done" or floor is None:
            return
        for c in s.scalars(select(Claim).where(Claim.run_id == self.run_id, Claim.stream == st.role,
                                               Claim.id > floor, Claim.corrects_claim_id.is_(None))):  # fmt: skip
            s.delete(c)

    # ------------------------------------------------------------------ stages
    async def _facts(self) -> None:
        """Deterministic facts established before any agent runs; kinds extend this."""
        from finresearch.fincalc.dates import today_ist

        facts = dict(self.ctx.facts)
        facts["today_ist"] = today_ist().isoformat()
        self.ctx.facts = facts
        self._update_manifest(facts=facts)

    def _claims_text(self, stream: str | None = None, ids: list[int] | None = None) -> str:
        with session_scope() as s:
            q = select(Claim).where(Claim.run_id == self.run_id)
            if ids is not None:
                q = q.where(Claim.id.in_(ids))
            else:
                q = q.where(Claim.stream == stream, Claim.importance.in_(self.config.verify_importance),
                            Claim.status.in_(("unverified", "needs_review")))  # fmt: skip
            rows = s.scalars(q.order_by(Claim.id)).all()
            return json.dumps([{"claim_id": c.id, "stream": c.stream, "statement": c.statement, "metric": c.metric,
                                "value": str(c.value) if c.value is not None else None, "unit": c.unit,
                                "period": c.period, "importance": c.importance, "status": c.status,
                                "gate_checks": c.checks or {}, "gate_notes": c.verifier_note,
                                "citations": [{"document_id": x.document_id, "lines": [x.line_start, x.line_end],
                                               "url": x.url, "quote": (x.quote or "")[:300]} for x in c.citations]}
                               for c in rows], indent=1)  # fmt: skip

    def _once(self, key: str, fn: Callable[[], None]) -> None:
        """Run post-processing for a step exactly once, even across resumes."""
        with session_scope() as s:
            run = s.get(ResearchRun, self.run_id)
            applied = list((run.manifest or {}).get("applied", []))
            if key in applied:
                return
        fn()
        with session_scope() as s:
            run = s.get(ResearchRun, self.run_id)
            m = dict(run.manifest or {})
            m["applied"] = [*m.get("applied", []), key]
            run.manifest = m

    def _gate(self, stream: str | None) -> dict[str, Any]:
        from finresearch.verify.gate import run_gate

        with session_scope() as s:
            return run_gate(s, self.run_id, stream=stream, facts=self.ctx.facts).summary()

    def _apply_verdicts(self, rep: VerificationReport, *, second_opinion: bool = False) -> None:
        from finresearch.verify.gate import apply_correction, is_deterministic

        with session_scope() as s:
            for v in rep.verdicts:
                c = s.get(Claim, v.claim_id)
                if c is None or c.run_id != self.run_id or c.status == "unsupported" or is_deterministic(c):
                    continue  # an exchange/AMFI fact is compared against, never re-judged by a model
                note = (f"correct: {v.correct_value}. " if v.correct_value else "") + v.evidence[:2000]
                if second_opinion:
                    # a high-importance claim stays verified only if the second, independent verifier agrees
                    if c.status == "verified" and v.verdict != "verified":
                        c.status = "needs_review"
                        c.verifier_note = f"{c.verifier_note or ''} | second verifier disagrees: {note}"[
                            :4000
                        ]
                    continue
                if c.status == "contradicted" and (c.checks or {}).get("day_label_ok") is False:
                    continue  # a deterministic contradiction is not overridden by a model
                c.status = v.verdict
                c.verifier_note = note
                if v.verdict == "contradicted":
                    apply_correction(s, c, v.correct_value, v.evidence)

    def _high_verified(self, stream: str) -> list[int]:
        with session_scope() as s:
            return list(s.scalars(select(Claim.id).where(Claim.run_id == self.run_id, Claim.stream == stream,
                                                         Claim.importance == "high", Claim.status == "verified")))  # fmt: skip

    async def _stream_and_verify(self, stream: str, key_prefix: str = "", focus: str = "") -> StreamReport:
        key = f"{key_prefix}stream:{stream}"
        rep = await self.step(key, "stream", stream, focus=focus or "see plan")
        self._once(f"gate:{key}", lambda: self._gate(stream))
        claims = self._claims_text(stream)
        if claims != "[]":
            vkey = f"{key_prefix}verify:{stream}"
            ver = await self.step(vkey, "verify", self.roles["verifier"], target_stream=stream, claims=claims)
            self._once(vkey, lambda: self._apply_verdicts(ver))
            high = self._high_verified(stream)
            if high:
                v2key = f"{key_prefix}verify2:{stream}"
                ver2 = await self.step(v2key, "verify", self.roles["verifier"], target_stream=f"{stream} (second opinion)",
                                       claims=self._claims_text(ids=high))  # fmt: skip
                self._once(v2key, lambda: self._apply_verdicts(ver2, second_opinion=True))
        return rep

    async def _cross_stream(self, key_prefix: str = "") -> None:
        """Whole-run conflict check; conflicting claims go to one verifier pass."""
        # the conflicts are kept in the manifest: a run paused before their verifier finished resumes past the gate
        mkey = f"conflicts_{key_prefix}cross"
        self._once(f"gate:{key_prefix}cross",
                   lambda: self._update_manifest(**{mkey: self._gate(None).get("conflicts", [])}))  # fmt: skip
        with session_scope() as s:
            conflicts = (s.get(ResearchRun, self.run_id).manifest or {}).get(mkey)
            if conflicts is None:  # gated before the conflicts were saved: they are still on the claims
                rows = s.scalars(select(Claim).where(Claim.run_id == self.run_id)).all()
                conflicts = [(c.id, o) for c in rows for o in (c.checks or {}).get("conflict_with", [])]
        ids = sorted({i for pair in conflicts for i in pair})
        if ids:
            key = f"{key_prefix}verify:cross-stream"
            ver = await self.step(key, "verify", self.roles["verifier"], target_stream="cross-stream conflicts",
                                  claims=self._claims_text(ids=ids))  # fmt: skip
            self._once(key, lambda: self._apply_verdicts(ver))

    def _identities(self, key: str) -> None:
        """Accounting-identity and scale checks (verify/identities.py) after the streams, before synthesis. Results
        go to the manifest and to each involved claim's checks; the publish gate applies the severity policy."""

        def go() -> None:
            from finresearch.verify.identities import run_identities

            with session_scope() as s:
                rep = run_identities(s, self.run_id, record=True).to_dict()
            self._update_manifest(identity_checks={"counts": rep["counts"], "applicable": rep["applicable"],
                                                   "failing": [x for x in rep["checks"] if x["status"] != "pass"][:40],
                                                   "not_enough_inputs": rep["not_enough_inputs"]})  # fmt: skip

        self._once(key, go)

    async def _synthesize(self, key: str, reports: dict[str, StreamReport], bull, bear) -> BaseModel:
        """Synthesis plus the publish gate, with up to max_revisions revision rounds."""
        from finresearch.verify.gate import check_report

        kwargs = {"stream_reports": _reports_text(reports), "bull": bull.model_dump_json(indent=1),
                  "bear": bear.model_dump_json(indent=1)}  # fmt: skip
        synth = await self.step(key, "synthesis", self.roles["synthesizer"], revision="none", **kwargs)
        for i in range(1, self.config.max_revisions + 1):
            with session_scope() as s:
                g = check_report(s, self.run_id, synth.report_markdown)
            self._update_manifest(**{f"gate_{key}": {"ok": g.ok, "blocking": g.blocking[:50],
                                                     "warnings": g.warnings[:50]}})  # fmt: skip
            if g.ok:
                break
            synth = await self.step(f"{key}:fix{i}", "synthesis", self.roles["synthesizer"],
                                    revision=g.revision_request(), **kwargs)  # fmt: skip
        return synth

    @staticmethod
    def _focus(plan: ResearchPlan, stream: str) -> str:
        sf = next((x for x in plan.streams if x.stream == stream), None)
        if sf is None:
            return "No specific focus from the planner; cover the stream fully."
        return (
            "Questions:\n- "
            + "\n- ".join(sf.questions)
            + ("\nLeads:\n- " + "\n- ".join(sf.leads) if sf.leads else "")
        )

    async def _gather(self, coros: list[Awaitable[Any]]) -> list[Any]:
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, RunPaused):
                raise r
        for r in results:
            if isinstance(r, BaseException):
                raise r
        return results

    def _restore_choice(self) -> None:
        """Streams and concurrency chosen when the run started are saved in the manifest; a resume that does not
        choose again (the app's resume button, `ipo resume <id>`) keeps them."""
        with session_scope() as s:
            m = s.get(ResearchRun, self.run_id).manifest or {}
        streams, concurrency = self._chosen
        if streams is None and m.get("streams"):
            self.config.streams = tuple(m["streams"])
        if concurrency is None and m.get("concurrency"):
            self.config.concurrency = int(m["concurrency"])
            self._sem = asyncio.Semaphore(self.config.concurrency)
        self._update_manifest(streams=list(self.config.streams), concurrency=self.config.concurrency)

    def _render(self) -> None:
        self._update_manifest(pack=_render_pack_safely(self.run_id))

    # ------------------------------------------------------------------ main
    async def run(self) -> str:
        """Returns "done", "blocked", "paused" or "failed"; never raises for a failed step (the reason is stored in
        the manifest as `last_error`, a pause's as `pause_reason`)."""
        self._paused = None
        try:
            self._set_run(status="running", resume_after=None)
            self._update_manifest(last_error=None, pause_reason=None, pause_kind=None)
            self.ctx = self._load_context()
            self._restore_choice()
            with session_scope() as s:
                self._transient_pauses = int(
                    (s.get(ResearchRun, self.run_id).manifest or {}).get("transient_pauses") or 0
                )
            self._update_manifest(pipeline=self.version, kind=self.kind, git_sha=_git_sha(),
                                  models={k.value: v for k, v in get_settings().claude_models.items()},
                                  prompt_hashes={n: r.prompt_hash() for n, r in ROLES.items()})  # fmt: skip
            if self.required_doc_kinds and not self._has_primary_docs(self.ctx.documents):
                if self.config.discover:
                    from finresearch.ingest.discover import discover

                    await discover(self.ctx.company_slug, log=lambda m: None, kind=self.discovery_kind)
                self.ctx = self._load_context(require_offer_doc=True)
            await self._facts()
            plan: ResearchPlan = await self.step("planner", "plan", self.roles["planner"])
            reports = dict(zip(self.config.streams, await self._gather(
                [self._stream_and_verify(s, focus=self._focus(plan, s)) for s in self.config.streams]), strict=True))  # fmt: skip
            await self._cross_stream()
            self._identities("identities")
            bull, bear = await self._gather([self.step("case:bull", "case", self.roles["bull"]),
                                             self.step("case:bear", "case", self.roles["bear"])])  # fmt: skip
            synth = await self._synthesize("synthesis", reports, bull, bear)
            for rnd in range(1, self.config.max_followup_rounds + 1):
                critic: CriticReport = await self.step(f"critic:r{rnd}", "critic", self.roles["critic"],
                                                       draft=synth.report_markdown)  # fmt: skip
                gaps = [g for g in critic.gaps if g.severity == "high" and g.stream in self.config.streams]
                if critic.ready_to_publish or not gaps:
                    break
                by_stream: dict[str, list[str]] = {}
                for g in gaps:
                    by_stream.setdefault(g.stream, []).append(g.task)
                extra = await self._gather([self._stream_and_verify(st, key_prefix=f"r{rnd}:",
                                                                    focus="FOLLOW-UP (critic):\n- " + "\n- ".join(t))
                                            for st, t in by_stream.items()])  # fmt: skip
                for st, rep in zip(by_stream, extra, strict=True):
                    reports[f"{st} (follow-up {rnd})"] = rep
                await self._cross_stream(key_prefix=f"r{rnd}:")
                self._identities(f"identities:r{rnd}")
                synth = await self._synthesize(f"synthesis:r{rnd}", reports, bull, bear)
            out = get_settings().runs_dir / str(self.run_id)
            out.mkdir(parents=True, exist_ok=True)
            from finresearch.verify.gate import check_report

            with session_scope() as s:
                final = check_report(s, self.run_id, synth.report_markdown)
            (out / "synthesis.json").write_text(synth.model_dump_json(indent=1))
            self._update_manifest(final_gate={"ok": final.ok, "blocking": final.blocking[:50],
                                              "warnings": final.warnings[:50]})  # fmt: skip
            status = "done" if final.ok else "blocked"
            if not final.ok:
                # never publish a report that relies on contradicted/unsupported/unverified-high claims
                (out / "report_blocked.md").write_text(synth.report_markdown)
            else:
                (out / "report.md").write_text(synth.report_markdown)
            if self.config.render:
                self._render()
            self._set_run(status=status, finished_at=_now())
            if status == "done":  # log the verdict as a checkable forecast (never fails the run)
                from finresearch.signals.ledger import record_run_safely

                record_run_safely(self.run_id)
            return status
        except RunPaused as p:
            self._set_run(status="paused", resume_after=p.resume_after)
            self._update_manifest(pause_reason=str(p), pause_kind=p.kind)
            return "paused"
        except (
            Exception
        ) as e:  # StepFailed or anything unexpected: record why and return, never kill the worker
            self._fail(e)
            return "failed"

    def _fail(self, e: BaseException) -> None:
        reason = str(e) if isinstance(e, StepFailed) else f"{type(e).__name__}: {e}"
        logger.error("run %s failed: %s", self.run_id, reason, exc_info=e)
        try:
            self._set_run(status="failed")
            self._update_manifest(last_error={"message": reason[:4000], "type": type(e).__name__,
                                              "at": _now().isoformat()})  # fmt: skip
            # a step left "running" by the failure would look like work in progress
            with session_scope() as s:
                for st in s.scalars(select(AgentStep).where(AgentStep.run_id == self.run_id,
                                                            AgentStep.status == "running")):  # fmt: skip
                    st.status, st.error, st.finished_at = (
                        "failed",
                        st.error or f"run failed: {reason[:1000]}",
                        _now(),
                    )
        except Exception:  # the database itself is unreachable (or the run is gone): the log keeps the reason
            logger.exception("run %s: could not record the failure", self.run_id)


def _render_pack_safely(run_id: int) -> dict[str, Any]:
    from finresearch.render.pack import render_pack

    try:
        r = render_pack(run_id)
        return {
            "path": str(r.path),
            "gate_ok": r.gate_ok,
            "pdf_pages": r.pdf_pages,
            "files": r.files,
            "notes": r.notes,
        }
    except Exception as e:  # rendering must never lose a finished run
        return {"error": f"{type(e).__name__}: {e}"}


def _reports_text(reports: dict[str, StreamReport]) -> str:
    parts = []
    for name, r in reports.items():
        parts.append(f"### Stream: {name}\nSummary: {r.summary}\nRed flags: {r.red_flags}\n"
                     f"Open questions: {r.open_questions}\n\n{r.section_markdown}")  # fmt: skip
    return "\n\n".join(parts)


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=get_settings().runs_dir.parent.parent, timeout=5).stdout.strip() or None  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None


class KindMismatch(ValueError):
    """The research kind does not suit the company's asset class (an IPO report on a mutual fund, ...)."""


# slug prefix of a non-equity asset class -> the only kind that researches it
ASSET_KINDS = {"mf-": "fund_report", "bond-": "bond_report"}


def check_kind(company_slug: str, kind: str) -> None:
    for prefix, only in ASSET_KINDS.items():
        if company_slug.startswith(prefix) and kind != only:
            raise KindMismatch(
                f"{company_slug!r} is a {only.split('_')[0]}; research it with {only!r}, not {kind!r}"
            )
        if kind == only and not company_slug.startswith(prefix):
            raise KindMismatch(
                f"{kind!r} needs a {only.split('_')[0]} (slug {prefix}...), not {company_slug!r}"
            )


def create_run(company_slug: str, kind: str = "ipo_report") -> int:
    from finresearch.orchestrator.kinds import KINDS

    if kind not in KINDS:
        raise ValueError(f"unknown research kind {kind!r}; choose from {sorted(KINDS)}")
    check_kind(company_slug, kind)
    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.slug == company_slug))
        if co is None:
            raise ValueError(f"unknown company {company_slug!r}; ingest its documents first")
        run = ResearchRun(company_id=co.id, kind=kind, status="running", manifest={})
        s.add(run)
        s.flush()
        return run.id


async def run_until_done(run_id: int, *, wait: bool, pipeline: ResearchPipeline | None = None, log=print,
                         sleep=asyncio.sleep, clock=time.time) -> str:  # fmt: skip
    """Run the pipeline; with wait=True, sleep through Max-window resets and resume automatically."""
    from finresearch.orchestrator.kinds import pipeline_for

    pipe = pipeline or pipeline_for(run_id)
    while True:
        status = await pipe.run()
        if status != "paused" or not wait:
            return status
        with session_scope() as s:
            run = s.get(ResearchRun, run_id)
            ra, why = run.resume_after, (run.manifest or {}).get("pause_reason")
        delay = max(60.0, (ra.timestamp() - clock()) + 60) if ra else 900.0
        log(f"run {run_id} paused ({why}); resuming in {delay / 60:.0f} min ({ra})")
        await sleep(delay)

"""IPO pipeline: ordering, persistence, resume after crash, pause on limits/budget, critic follow-ups."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from finresearch.agents.schemas import (
    CaseReport,
    ClaimVerdict,
    CriticReport,
    Gap,
    ResearchPlan,
    StreamFocus,
    StreamReport,
    Synthesis,
    VerificationReport,
)
from finresearch.bridge.limits import LimitTracker
from finresearch.bridge.router import AllTiersFailed
from finresearch.bridge.types import AgentResult, RateLimitSnapshot, Tier

FIX = Path(__file__).parent / "fixtures"
STREAMS = ("financials", "risks", "news30")  # smaller stream set keeps tests fast


@pytest.fixture
def company_run(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Document, DocumentPage
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator.ipo import create_run

    txt = tmp_path / "text.txt"
    shutil.copy(FIX / "orient_rhp_pnl_layout.txt", txt)
    slug = "acme-" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:8]
    with session_scope() as s:
        co = get_or_create_company(s, slug, "Acme Cables Ltd")
        d = Document(company_id=co.id, kind="RHP", title="Acme RHP", sha256=hashlib.sha256(str(tmp_path).encode())
                     .hexdigest(), local_path=str(txt), text_path=str(txt), bytes=1, pages=1)  # fmt: skip
        s.add(d)
        s.flush()
        s.add(DocumentPage(document_id=d.id, page_no=1, line_start=1, line_end=40, text="", char_count=0,
                           text_source="pdftotext"))  # fmt: skip
    return create_run(slug)


class FakeRunner:
    """Returns canned role outputs; streams save one claim each so verification has work to do."""

    def __init__(self, run_id, *, fail_on=None, limit_on=None, tracker=None, critic_gap_rounds=0):
        self.run_id, self.calls, self.fail_on, self.limit_on = (
            run_id,
            [],
            set(fail_on or []),
            set(limit_on or []),
        )
        self.tracker, self.critic_gap_rounds, self.critic_calls = tracker, critic_gap_rounds, 0

    async def __call__(self, role, ctx, **extra):
        from finresearch.db import session_scope
        from finresearch.db.models import Claim

        key = role if role not in ("verifier",) else f"verifier:{extra.get('target_stream')}"
        self.calls.append(key)
        if key in self.fail_on:
            self.fail_on.discard(key)
            raise RuntimeError(f"crash in {key}")
        if key in self.limit_on:
            self.limit_on.discard(key)
            self.tracker.record_limit(Tier.CLAUDE_MAX, int(time.time()) + 120, "limit")
            raise AllTiersFailed(key, ["claude_max:limit", "local:skipped(allow_degraded=False)"])
        await asyncio.sleep(0)
        if role == "planner":
            out = ResearchPlan(company_one_liner="x", critical_questions=["q"],
                               streams=[StreamFocus(stream=s, questions=[f"{s}?"]) for s in STREAMS])  # fmt: skip
        elif role in STREAMS:
            with session_scope() as s:
                c = Claim(run_id=self.run_id, stream=role, statement=f"{role} fact", claim_type="factual")
                s.add(c)
                s.flush()
                cid = c.id
            out = StreamReport(
                summary=role, section_markdown=f"{role} [C{cid}]", key_findings=[], claim_ids=[cid]
            )
        elif role == "verifier":
            import json

            ids = [c["claim_id"] for c in json.loads(extra["claims"])]
            out = VerificationReport(verdicts=[ClaimVerdict(claim_id=i, verdict="verified", evidence="ok") for i in ids],
                                     summary="ok")  # fmt: skip
        elif role in ("bull", "bear"):
            out = CaseReport(
                thesis=role, points=[], listing_view="-", long_term_view="-", strongest_counterargument="-"
            )
        elif role == "synthesizer":
            out = Synthesis(verdict_listing="-", verdict_long_term="-", overall_verdict="NEUTRAL", confidence="medium",
                            executive_summary="-", reasons_for=[], reasons_against=[], scenarios=[],
                            action_checklist=[], report_markdown=f"# Report\n{extra['stream_reports'][:50]}")  # fmt: skip
        elif role == "critic":
            self.critic_calls += 1
            gaps = [Gap(description="missing", stream="risks", task="check litigation amounts", severity="high")] \
                if self.critic_calls <= self.critic_gap_rounds else []  # fmt: skip
            out = CriticReport(gaps=gaps, ready_to_publish=not gaps)
        else:
            raise AssertionError(role)
        res = AgentResult(task_name=key, tier=Tier.CLAUDE_MAX, model="fake", ok=True, structured_output={},
                          rate_limit=RateLimitSnapshot(five_hour_utilization=0.10, five_hour_resets_at=int(time.time()) + 3600))  # fmt: skip
        return out, res


def make(run_id, tmp_path, runner, tracker=None):
    from finresearch.orchestrator.ipo import IpoPipeline, PipelineConfig

    tracker = tracker or LimitTracker(tmp_path / "limits")
    runner.tracker = runner.tracker or tracker
    return IpoPipeline(
        run_id, runner=runner, tracker=tracker, config=PipelineConfig(streams=STREAMS, concurrency=2)
    )


def steps(run_id):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep

    with session_scope() as s:
        return {x.key: x.status for x in s.scalars(select(AgentStep).where(AgentStep.run_id == run_id))}


def run_row(run_id):
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun

    with session_scope() as s:
        r = s.get(ResearchRun, run_id)
        return r.status, r.resume_after, dict(r.manifest)


async def test_happy_path_order_persistence_and_report(company_run, tmp_path, env):
    runner = FakeRunner(company_run)
    assert await make(company_run, tmp_path, runner).run() == "done"
    st = steps(company_run)
    assert all(v == "done" for v in st.values())
    assert {"planner", "case:bull", "case:bear", "synthesis", "critic:r1"} <= set(st)
    assert all(f"stream:{s}" in st and f"verify:{s}" in st for s in STREAMS)
    c = runner.calls
    for s in STREAMS:
        assert c.index(s) < c.index(f"verifier:{s}") < min(c.index("bull"), c.index("bear"))
    assert c.index("planner") == 0 and c.index("synthesizer") > max(c.index("bull"), c.index("bear"))
    status, _, manifest = run_row(company_run)
    assert (
        status == "done" and "prompt_hashes" in manifest and manifest["pipeline"].startswith("ipo-pipeline")
    )
    assert (env.runs_dir / str(company_run) / "report.md").exists()
    from finresearch.db import session_scope
    from finresearch.db.models import Claim

    with session_scope() as s:
        assert {c.status for c in s.scalars(select(Claim).where(Claim.run_id == company_run))} == {"verified"}


async def test_resume_after_crash_skips_finished_steps(company_run, tmp_path):
    runner = FakeRunner(company_run, fail_on={"bear"})
    assert (
        await make(company_run, tmp_path, runner).run() == "failed"
    )  # recorded, not raised: the worker exits cleanly
    status, _, manifest = run_row(company_run)
    assert status == "failed" and steps(company_run)["case:bear"] == "failed"
    assert (
        "crash in bear" in manifest["last_error"]["message"]
        and manifest["last_error"]["type"] == "StepFailed"
    )
    runner2 = FakeRunner(company_run)
    assert await make(company_run, tmp_path, runner2).run() == "done"
    assert "planner" not in runner2.calls and not set(STREAMS) & set(runner2.calls)
    assert "bear" in runner2.calls and "synthesizer" in runner2.calls


async def test_limit_pauses_then_run_until_done_resumes(company_run, tmp_path):
    from finresearch.orchestrator.ipo import run_until_done

    tracker = LimitTracker(tmp_path / "limits")
    runner = FakeRunner(company_run, limit_on={"news30"}, tracker=tracker)
    pipe = make(company_run, tmp_path, runner, tracker)
    assert await pipe.run() == "paused"
    status, resume_after, _ = run_row(company_run)
    assert status == "paused" and resume_after is not None
    assert steps(company_run)["stream:news30"] == "deferred"

    slept = []

    async def fake_sleep(d):
        slept.append(d)
        tracker.reset(Tier.CLAUDE_MAX)  # window has reset

    assert (
        await run_until_done(company_run, wait=True, pipeline=pipe, sleep=fake_sleep, log=lambda m: None)
        == "done"
    )
    assert slept and steps(company_run)["stream:news30"] == "done"
    assert runner.calls.count("financials") == 1  # finished streams were not redone


async def test_budget_preflight_defers_before_calling_claude(company_run, tmp_path):
    tracker = LimitTracker(tmp_path / "limits", five_hour_ceiling=0.99)
    tracker.record_snapshot(Tier.CLAUDE_MAX, RateLimitSnapshot(status="allowed", five_hour_utilization=0.9,
                                                               five_hour_resets_at=int(time.time()) + 1800))  # fmt: skip
    runner = FakeRunner(company_run, tracker=tracker)
    assert await make(company_run, tmp_path, runner, tracker).run() == "paused"
    assert runner.calls == [] and steps(company_run)["planner"] == "deferred"
    assert run_row(company_run)[1] is not None


async def test_critic_gap_triggers_follow_up_round(company_run, tmp_path):
    runner = FakeRunner(company_run, critic_gap_rounds=1)
    assert await make(company_run, tmp_path, runner).run() == "done"
    st = steps(company_run)
    assert {"r1:stream:risks", "r1:verify:risks", "synthesis:r1", "critic:r2"} <= set(st)
    assert "r2:stream:risks" not in st


class GateRunner(FakeRunner):
    """Adds high-importance claims, a disagreeing second verifier and bad synthesizer drafts."""

    def __init__(self, run_id, *, bad_drafts=0, second_disagrees=False, **kw):
        super().__init__(run_id, **kw)
        self.bad_drafts, self.second_disagrees, self.revisions = bad_drafts, second_disagrees, []

    async def __call__(self, role, ctx, **extra):
        import json

        from finresearch.db import session_scope
        from finresearch.db.models import Claim

        if role == "synthesizer":
            self.calls.append("synthesizer")
            self.revisions.append(extra.get("revision"))
            with session_scope() as s:
                ids = [
                    c.id
                    for c in s.scalars(select(Claim).where(Claim.run_id == self.run_id).order_by(Claim.id))
                ]
            cite = "[C999999]" if self.bad_drafts > 0 else f"[C{ids[0]}]"
            self.bad_drafts -= 1
            out = Synthesis(verdict_listing="-", verdict_long_term="-", overall_verdict="NEUTRAL", confidence="medium",
                            executive_summary="-", reasons_for=[], reasons_against=[], scenarios=[],
                            action_checklist=[], report_markdown=f"# Report\nRevenue ₹1,171.65 cr {cite}.\n")  # fmt: skip
            return out, AgentResult(task_name="s", tier=Tier.CLAUDE_MAX, model="fake", ok=True)
        if role == "verifier" and "second opinion" in extra.get("target_stream", ""):
            self.calls.append("verifier:second")
            ids = [c["claim_id"] for c in json.loads(extra["claims"])]
            verdict = "contradicted" if self.second_disagrees else "verified"
            out = VerificationReport(verdicts=[ClaimVerdict(claim_id=i, verdict=verdict, evidence="2nd") for i in ids],
                                     summary="2nd")  # fmt: skip
            return out, AgentResult(task_name="v2", tier=Tier.CLAUDE_MAX, model="fake", ok=True)
        out, res = await super().__call__(role, ctx, **extra)
        if role in STREAMS:
            with session_scope() as s:
                s.get(Claim, out.claim_ids[0]).importance = "high"
        return out, res


async def test_publish_gate_requests_a_revision_then_publishes(company_run, tmp_path, env):
    runner = GateRunner(company_run, bad_drafts=1)
    assert await make(company_run, tmp_path, runner).run() == "done"
    st = steps(company_run)
    assert st["synthesis:fix1"] == "done" and "synthesis:fix2" not in st
    assert runner.revisions[0] == "none" and runner.revisions[1].startswith("REVISION REQUIRED")
    assert "[C999999] does not exist" in runner.revisions[1]
    assert (env.runs_dir / str(company_run) / "report.md").exists()
    assert run_row(company_run)[2]["final_gate"]["ok"] is True


async def test_report_stays_blocked_when_revisions_do_not_fix_it(company_run, tmp_path, env):
    runner = GateRunner(company_run, bad_drafts=99)
    assert await make(company_run, tmp_path, runner).run() == "blocked"
    out = env.runs_dir / str(company_run)
    assert (out / "report_blocked.md").exists() and not (out / "report.md").exists()
    status, _, manifest = run_row(company_run)
    assert status == "blocked" and manifest["final_gate"]["ok"] is False


async def test_second_verifier_disagreement_downgrades_high_importance_claims(company_run, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim

    runner = GateRunner(company_run, second_disagrees=True, bad_drafts=0)
    status = await make(company_run, tmp_path, runner).run()
    assert "verifier:second" in runner.calls and all(f"verify2:{s}" in steps(company_run) for s in STREAMS)
    with session_scope() as s:
        highs = s.scalars(select(Claim).where(Claim.run_id == company_run, Claim.importance == "high")).all()
        assert highs and all(
            c.status == "needs_review" and "second verifier disagrees" in c.verifier_note for c in highs
        )
    assert status == "blocked"  # the report cites a high-importance claim that is no longer verified


# --------------------------------------------------------------------------- research kinds
def test_kinds_registry_maps_runs_to_pipelines(company_run):
    from finresearch.orchestrator.ipo import IpoPipeline, create_run
    from finresearch.orchestrator.kinds import KINDS, pipeline_for

    assert KINDS["ipo_report"] is IpoPipeline and IpoPipeline.default_streams == (
        "financials", "business", "risks", "valuation", "news30", "demand", "major")  # fmt: skip
    pipe = pipeline_for(company_run)
    assert isinstance(pipe, IpoPipeline) and pipe.config.streams == IpoPipeline.default_streams
    with pytest.raises(ValueError, match="unknown research kind"):
        create_run("anything", kind="crypto")


async def test_a_new_kind_is_configuration_not_a_new_orchestrator(env, tmp_path, monkeypatch):
    """A kind with its own streams and no required documents runs the whole shared DAG."""
    from finresearch.db import session_scope
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator import kinds
    from finresearch.orchestrator.base import PipelineConfig, ResearchPipeline, create_run

    class NotesKind(ResearchPipeline):
        kind = "notes_report"
        default_streams = ("financials", "news30")

    monkeypatch.setitem(kinds.KINDS, NotesKind.kind, NotesKind)
    with session_scope() as s:
        get_or_create_company(s, "notes-" + tmp_path.name[-8:], "Notes Co")
    run_id = create_run("notes-" + tmp_path.name[-8:], kind="notes_report")
    runner = FakeRunner(run_id)
    pipe = kinds.pipeline_for(run_id, runner=runner, tracker=LimitTracker(tmp_path / "limits"),
                              config=PipelineConfig(concurrency=2, render=False))  # fmt: skip
    assert type(pipe) is NotesKind
    status = await pipe.run()
    st = steps(run_id)
    assert status == "done" and {"planner", "stream:financials", "stream:news30", "synthesis"} <= set(st)
    assert "stream:risks" not in st and "baseline" not in str(st)
    from finresearch.db.models import ResearchRun

    with session_scope() as s:
        m = s.get(ResearchRun, run_id).manifest
    assert (
        m["kind"] == "notes_report" and m["pipeline"] == "research-pipeline-1" and "today_ist" in m["facts"]
    )


# --------------------------------------------------------------------------- review fixes
def _claims(run_id, stream=None):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim

    with session_scope() as s:
        q = select(Claim).where(Claim.run_id == run_id)
        if stream:
            q = q.where(Claim.stream == stream)
        return [(c.id, c.statement, c.status) for c in s.scalars(q.order_by(Claim.id))]


class ConflictRunner(FakeRunner):
    """financials and risks state the same metric + period with different values: a cross-stream conflict."""

    async def __call__(self, role, ctx, **extra):
        out, res = await super().__call__(role, ctx, **extra)
        if role in ("financials", "risks"):
            from decimal import Decimal

            from finresearch.db import session_scope
            from finresearch.db.models import Claim

            with session_scope() as s:
                s.add(Claim(run_id=self.run_id, stream=role, statement=f"{role} PAT", claim_type="numeric",
                            metric="pat", value=Decimal("535.61" if role == "financials" else "600"),
                            unit="INR million", period="FY2026"))  # fmt: skip
        return out, res


async def test_cross_stream_conflicts_are_verified_after_a_resume(company_run, tmp_path):
    tracker = LimitTracker(tmp_path / "limits")
    runner = ConflictRunner(company_run, limit_on={"verifier:cross-stream conflicts"}, tracker=tracker)
    assert await make(company_run, tmp_path, runner, tracker).run() == "paused"
    assert steps(company_run)["verify:cross-stream"] == "deferred"
    tracker.reset(Tier.CLAUDE_MAX)
    runner2 = ConflictRunner(company_run, tracker=tracker)
    assert await make(company_run, tmp_path, runner2, tracker).run() == "done"
    assert "verifier:cross-stream conflicts" in runner2.calls
    assert steps(company_run)["verify:cross-stream"] == "done"


async def test_a_rerun_stream_step_replaces_its_earlier_claims(company_run, tmp_path):
    class HalfRunner(FakeRunner):
        """risks saves a claim, then crashes on its first attempt."""

        async def __call__(self, role, ctx, **extra):
            if role == "risks" and "risks" in self.fail_on:
                from finresearch.db import session_scope
                from finresearch.db.models import Citation, Claim

                with session_scope() as s:
                    c = Claim(
                        run_id=self.run_id, stream="risks", statement="half-saved", claim_type="factual"
                    )
                    s.add(c)
                    s.flush()
                    s.add(Citation(claim_id=c.id, url="https://x.example", quote="q"))
            return await super().__call__(role, ctx, **extra)

    assert await make(company_run, tmp_path, HalfRunner(company_run, fail_on={"risks"})).run() == "failed"
    assert steps(company_run)["stream:risks"] == "failed"  # the crash left a half-saved claim behind
    runner2 = FakeRunner(company_run)
    assert await make(company_run, tmp_path, runner2).run() == "done"
    assert runner2.calls.count("risks") == 1
    assert [x[1] for x in _claims(company_run, "risks")] == ["risks fact"]
    assert len(_claims(company_run, "financials")) == 1


async def test_a_follow_up_rerun_keeps_the_first_rounds_claims(company_run, tmp_path):
    class FollowUpCrash(FakeRunner):
        """The follow-up risks stream saves a claim, then crashes."""

        async def __call__(self, role, ctx, **extra):
            if role == "risks" and self.calls.count("risks") == 1 and self.fail_on:
                from finresearch.db import session_scope
                from finresearch.db.models import Claim

                self.fail_on.clear()
                with session_scope() as s:
                    s.add(
                        Claim(
                            run_id=self.run_id, stream="risks", statement="half-saved", claim_type="factual"
                        )
                    )
                raise RuntimeError("crash in the follow-up")
            return await super().__call__(role, ctx, **extra)

    pipe = make(company_run, tmp_path, FollowUpCrash(company_run, critic_gap_rounds=1, fail_on={"x"}))
    assert await pipe.run() == "failed"
    assert steps(company_run)["r1:stream:risks"] == "failed"
    before = [x for x in _claims(company_run, "risks") if x[1] != "half-saved"]
    assert len(before) == 1 and before[0][2] == "verified"
    runner2 = FakeRunner(company_run)  # critic:r1 is done; critic:r2 finds no gaps
    assert await make(company_run, tmp_path, runner2).run() == "done"
    assert "r2:stream:risks" not in steps(company_run)
    after = _claims(company_run, "risks")
    assert after[0] == before[0] and [x[1] for x in after] == ["risks fact", "risks fact"]


async def test_resume_keeps_the_streams_chosen_at_start(company_run, tmp_path):
    from finresearch.orchestrator.ipo import IpoPipeline, PipelineConfig

    runner = FakeRunner(company_run, fail_on={"bear"})
    assert await make(company_run, tmp_path, runner).run() == "failed"
    manifest = run_row(company_run)[2]
    assert manifest["streams"] == list(STREAMS) and manifest["concurrency"] == 2
    runner2 = FakeRunner(company_run)
    pipe = IpoPipeline(
        company_run, runner=runner2, tracker=LimitTracker(tmp_path / "limits"), config=PipelineConfig()
    )
    assert await pipe.run() == "done"
    assert pipe.config.streams == STREAMS and pipe.config.concurrency == 2
    assert not {k for k in steps(company_run) if k.startswith("stream:")} - {f"stream:{s}" for s in STREAMS}


async def test_bidding_day_is_recomputed_on_every_run(company_run, tmp_path, monkeypatch):
    from datetime import date

    from finresearch.adapters import nse_holidays
    from finresearch.db import session_scope
    from finresearch.db.models import Company, ResearchRun
    from finresearch.fincalc import dates
    from finresearch.mcp_server import server

    with session_scope() as s:
        run = s.get(ResearchRun, company_run)
        s.get(Company, run.company_id).nse_symbol = "ACME"
        run.manifest = {"facts": {"issue_info": {"Issue Period": "25-Sep-2026 to 29-Sep-2026"},
                                  "issue_open": "2026-09-25", "issue_close": "2026-09-29", "bidding_day_today": 1}}  # fmt: skip

    async def no_network(symbol):
        raise AssertionError("issue information is already in the manifest")

    monkeypatch.setattr(server, "nse_ipo_detail", no_network)
    monkeypatch.setattr(nse_holidays, "trading_holidays", lambda *a, **k: set())
    monkeypatch.setattr(dates, "today_ist", lambda: date(2026, 9, 29))
    pipe = make(company_run, tmp_path, FakeRunner(company_run))
    pipe.ctx = pipe._load_context()
    await pipe._facts()
    assert pipe.ctx.facts["bidding_day_today"] == 3 and pipe.ctx.facts["today_ist"] == "2026-09-29"
    assert run_row(company_run)[2]["facts"]["bidding_day_today"] == 3


def test_create_run_refuses_mismatched_asset_classes(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator.base import KindMismatch, create_run

    tag = tmp_path.name[-8:]
    with session_scope() as s:
        for slug in (f"mf-{tag}", f"bond-{tag}", f"eq-{tag}"):
            get_or_create_company(s, slug, slug)
    for slug, kind in ((f"mf-{tag}", "ipo_report"), (f"mf-{tag}", "bond_report"), (f"bond-{tag}", "stock_report"),
                       (f"eq-{tag}", "fund_report"), (f"eq-{tag}", "bond_report")):  # fmt: skip
        with pytest.raises(KindMismatch):
            create_run(slug, kind=kind)
    assert create_run(f"mf-{tag}", kind="fund_report") and create_run(f"bond-{tag}", kind="bond_report")
    assert create_run(f"eq-{tag}", kind="ipo_report") and create_run(f"eq-{tag}", kind="stock_report")


def test_a_verifier_cannot_overrule_a_deterministic_exchange_fact(company_run, tmp_path):
    from decimal import Decimal

    from finresearch.db import session_scope
    from finresearch.db.models import Claim

    with session_scope() as s:
        c = Claim(run_id=company_run, stream="facts", statement="Bid lot is 55 equity shares", claim_type="numeric",
                  metric="lot_size", value=Decimal(55), unit="shares", period="offer", status="verified",
                  checks={"source": "nse_issue_info"})  # fmt: skip
        s.add(c)
        s.flush()
        cid = c.id
    pipe = make(company_run, tmp_path, FakeRunner(company_run))
    pipe._apply_verdicts(VerificationReport(verdicts=[ClaimVerdict(claim_id=cid, verdict="contradicted",
                                                                   evidence="54 per the RHP", correct_value="54")],
                                            summary="x"))  # fmt: skip
    assert _claims(company_run, "facts") == [(cid, "Bid lot is 55 equity shares", "verified")]


# ---------------------------------------------------------------- transient failures (run 13, 2026-09-30)
OAUTH = ("Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh. "
         "This is usually transient; retry in a minute")  # fmt: skip
SLEPT = "API Error: Your computer went to sleep mid-response. The response above may be incomplete."


def transient(key, text, kind):
    from finresearch.bridge.types import TransientError

    err = TransientError(f"run1-{key}: {text}", kind=kind)
    return AllTiersFailed(key, [f"claude_max:transient({err})", "local:skipped(allow_degraded=False)"], [err])


class FlakyRunner(FakeRunner):
    """Raises the queued errors for a role, one per call, then behaves like FakeRunner. A stream saves a claim
    before failing, as a real stream that dies mid-response does."""

    def __init__(self, run_id, errors: dict[str, list[BaseException]], **kw):
        super().__init__(run_id, **kw)
        self.errors = {k: list(v) for k, v in errors.items()}

    async def __call__(self, role, ctx, **extra):
        queued = self.errors.get(role)
        if queued:
            self.calls.append(role)
            if role in STREAMS:
                from finresearch.db import session_scope
                from finresearch.db.models import Claim

                with session_scope() as s:
                    s.add(
                        Claim(run_id=self.run_id, stream=role, statement="half-saved", claim_type="factual")
                    )
            raise queued.pop(0)
        return await super().__call__(role, ctx, **extra)


def flaky(run_id, tmp_path, runner, **cfg):
    from finresearch.orchestrator.ipo import IpoPipeline, PipelineConfig

    slept: list[float] = []

    async def sleep(d):
        slept.append(d)

    tracker = LimitTracker(tmp_path / "limits")
    runner.tracker = tracker
    pipe = IpoPipeline(run_id, runner=runner, tracker=tracker, sleep=sleep,
                       config=PipelineConfig(streams=STREAMS, concurrency=2, **cfg))  # fmt: skip
    return pipe, slept


def step_row(run_id, key):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep

    with session_scope() as s:
        st = s.scalar(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.key == key))
        return st.status, st.attempts, st.error


async def test_oauth_collision_is_retried_in_place_after_30_to_90_seconds(company_run, tmp_path):
    from finresearch.bridge.types import TaskFailed

    # the second one is how the CLI's message reached the pipeline before: a TaskFailed from the router
    runner = FlakyRunner(
        company_run, {"risks": [transient("risks", OAUTH, "oauth"), TaskFailed(f"x: {OAUTH}")]}
    )
    pipe, slept = flaky(company_run, tmp_path, runner)
    assert await pipe.run() == "done"
    assert len(slept) == 2 and all(30 <= d <= 90 for d in slept)
    status, attempts, error = step_row(company_run, "stream:risks")
    assert status == "done" and attempts == 3 and error is None
    assert [x[1] for x in _claims(company_run, "risks")] == ["risks fact"]  # failed attempts' claims dropped


async def test_backoff_grows_exponentially_with_jitter(company_run, tmp_path):
    errs = [transient("news30", "read ECONNRESET", "network") for _ in range(3)]
    pipe, slept = flaky(company_run, tmp_path, FlakyRunner(company_run, {"news30": errs}), retry_base_s=10)
    assert await pipe.run() == "done"
    assert 10 <= slept[0] <= 15 and 20 <= slept[1] <= 30 and 40 <= slept[2] <= 60


async def test_exhausted_retries_pause_the_run_with_a_reason_then_resume(company_run, tmp_path):
    from finresearch.orchestrator.ipo import run_until_done

    errs = [transient("financials", SLEPT, "sleep") for _ in range(8)]  # twice (1 try + 3 retries)
    runner = FlakyRunner(company_run, {"financials": errs})
    pipe, _ = flaky(company_run, tmp_path, runner)
    t0 = time.time()
    assert await pipe.run() == "paused"
    status, resume_after, m = run_row(company_run)
    assert status == "paused" and m["pause_kind"] == "transient" and m["transient_pauses"] == 1
    assert 590 <= resume_after.timestamp() - t0 <= 620  # transient_pause_s
    assert "the Mac went to sleep mid-response" in m["pause_reason"] and "went to sleep" in m["pause_reason"]
    assert step_row(company_run, "stream:financials")[0] == "deferred"

    waits = []

    async def fake_sleep(d):
        waits.append(d)

    assert (
        await run_until_done(company_run, wait=True, pipeline=pipe, sleep=fake_sleep, log=lambda m: None)
        == "done"
    )
    _, _, m = run_row(company_run)
    assert waits and m["pause_reason"] is None and m["transient_pauses"] == 0 and m["last_error"] is None


async def test_repeated_transient_pauses_end_in_a_failed_run(company_run, tmp_path):
    errs = [transient("financials", OAUTH, "oauth") for _ in range(20)]
    runner = FlakyRunner(company_run, {"financials": errs})
    pipe, _ = flaky(company_run, tmp_path, runner, transient_retries=0, max_transient_pauses=2)
    assert [await pipe.run() for _ in range(3)] == ["paused", "paused", "failed"]
    status, _, m = run_row(company_run)
    assert status == "failed" and "sign in again" in m["last_error"]["message"]
    assert step_row(company_run, "stream:financials")[0] == "failed"


async def test_an_open_circuit_pauses_until_the_cool_down(company_run, tmp_path):
    runner = FlakyRunner(
        company_run, {"risks": [AllTiersFailed("risks", ["claude_max:skipped(circuit open)"])]}
    )
    pipe, _ = flaky(company_run, tmp_path, runner)

    async def open_circuit(role, ctx, **extra):
        if role == "risks":
            pipe.tracker.record_limit(
                Tier.CLAUDE_MAX, int(time.time()) + 300, "circuit open after 3 failures: x"
            )
        return await FlakyRunner.__call__(runner, role, ctx, **extra)

    pipe.runner = open_circuit
    assert await pipe.run() == "paused"
    _, resume_after, m = run_row(company_run)
    assert m["pause_kind"] == "transient" and "circuit open" in m["pause_reason"]
    assert 250 <= resume_after.timestamp() - time.time() <= 310


async def test_a_step_timeout_fails_the_step_without_retrying(company_run, tmp_path):
    runner = FlakyRunner(company_run, {"bear": [transient("bear", "timed out after 1800s", "timeout")]})
    pipe, slept = flaky(company_run, tmp_path, runner)
    assert await pipe.run() == "failed" and slept == []
    assert "timed out" in run_row(company_run)[2]["last_error"]["message"]


@pytest.mark.parametrize("error", [KeyError("boom"), ValueError("bad output"), RuntimeError("?")])
async def test_an_unexpected_step_error_fails_the_run_cleanly_and_resume_recovers(
    company_run, tmp_path, error
):
    from finresearch.bridge.types import TaskFailed

    runner = FlakyRunner(
        company_run, {"risks": [error], "bull": [TaskFailed("run1-bull: permission denied")]}
    )
    pipe, _ = flaky(company_run, tmp_path, runner)
    assert await pipe.run() == "failed"  # no exception reaches the worker
    status, _, m = run_row(company_run)
    assert status == "failed" and type(error).__name__ in m["last_error"]["message"]
    assert step_row(company_run, "stream:risks")[0] == "failed"
    assert "running" not in steps(company_run).values()
    runner.errors.clear()
    assert await pipe.run() == "done"
    assert run_row(company_run)[2]["last_error"] is None


async def test_a_schema_violation_is_retried_once(company_run, tmp_path):
    from finresearch.bridge.types import SchemaViolation

    def sv():
        return AllTiersFailed(
            "x", ["claude_max:SchemaViolation(no structured_output)"], [SchemaViolation("no so")]
        )

    pipe, slept = flaky(company_run, tmp_path, FlakyRunner(company_run, {"critic": [sv()]}))
    assert await pipe.run() == "done" and len(slept) == 1
    pipe2, _ = flaky(company_run, tmp_path, FlakyRunner(company_run, {"critic": [sv(), sv()]}))
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep

    with session_scope() as s:  # run the critic again
        s.delete(
            s.scalar(select(AgentStep).where(AgentStep.run_id == company_run, AgentStep.key == "critic:r1"))
        )
    assert await pipe2.run() == "failed"

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
    with pytest.raises(RuntimeError):
        await make(company_run, tmp_path, runner).run()
    assert run_row(company_run)[0] == "failed"
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

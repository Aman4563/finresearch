"""#505: caps on the free text one agent hands to the next agent's prompt, and what happens when one is exceeded.

The planner's questions and leads, a stream's summary, red flags and open questions, the bull and bear cases and the
critic's tasks are pasted into the next agent's prompt. Each is capped in agents/schemas.py; the caps reach the CLI
through --json-schema and are checked again by Pydantic.
"""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_orchestrator import FakeRunner, company_run, make, run_row, steps  # noqa: F401  (fixture)

from finresearch.agents import schemas as S
from finresearch.agents.roles import ROLES
from finresearch.agents.schemas import json_schema_for

FIX = Path(__file__).parent / "fixtures"
LINE, ITEMS = S.HANDOFF_LINE_CHARS, S.HANDOFF_LIST_ITEMS


def _focus(**kw) -> dict:
    return {"stream": "risks", "questions": ["q"], **kw}


def _stream(**kw) -> dict:
    return {"summary": "s", "section_markdown": "m", "key_findings": [], **kw}


def _case(**kw) -> dict:
    return {"thesis": "t", "points": [], "listing_view": "l", "long_term_view": "v", "strongest_counterargument": "c",
            **kw}  # fmt: skip


def _gap(**kw) -> dict:
    return {"description": "d", "stream": "risks", "task": "t", "severity": "high", **kw}


def _point(text: str) -> dict:
    return {"point": text, "weight": "high"}


# (model, where the cap sits in its JSON schema, the cap, an output at the cap, an output one over it)
STRINGS = [
    (S.StreamFocus, ("questions", "items"), LINE, lambda n: _focus(questions=["x" * n])),
    (S.StreamFocus, ("leads", "items"), LINE, lambda n: _focus(leads=["x" * n])),
    (S.StreamReport, ("red_flags", "items"), LINE, lambda n: _stream(red_flags=["x" * n])),
    (S.StreamReport, ("open_questions", "items"), LINE, lambda n: _stream(open_questions=["x" * n])),
    (S.StreamReport, ("summary",), S.HANDOFF_SUMMARY_CHARS, lambda n: _stream(summary="x" * n)),
    (S.Gap, ("task",), S.HANDOFF_TASK_CHARS, lambda n: _gap(task="x" * n)),
    (S.CaseReport, ("thesis",), S.HANDOFF_VIEW_CHARS, lambda n: _case(thesis="x" * n)),
    (S.CaseReport, ("listing_view",), S.HANDOFF_VIEW_CHARS, lambda n: _case(listing_view="x" * n)),
    (S.CaseReport, ("long_term_view",), S.HANDOFF_VIEW_CHARS, lambda n: _case(long_term_view="x" * n)),
    (S.CaseReport, ("strongest_counterargument",), S.HANDOFF_VIEW_CHARS,
     lambda n: _case(strongest_counterargument="x" * n)),
    (S.CaseReport, ("points", "items", "properties", "point"), LINE, lambda n: _case(points=[_point("x" * n)])),
]  # fmt: skip
LISTS = [
    (S.StreamFocus, "questions", ITEMS, lambda n: _focus(questions=["q"] * n)),
    (S.StreamFocus, "leads", ITEMS, lambda n: _focus(leads=["l"] * n)),
    (S.StreamReport, "red_flags", ITEMS, lambda n: _stream(red_flags=["r"] * n)),
    (S.StreamReport, "open_questions", ITEMS, lambda n: _stream(open_questions=["o"] * n)),
    (S.CaseReport, "points", ITEMS, lambda n: _case(points=[_point("p")] * n)),
    (S.CriticReport, "gaps", ITEMS, lambda n: {"gaps": [_gap()] * n, "ready_to_publish": False}),
    (S.ResearchPlan, "streams", S.PLAN_STREAMS,
     lambda n: {"company_one_liner": "x", "critical_questions": ["q"], "streams": [_focus()] * n}),
]  # fmt: skip


def _id(x):
    return getattr(x, "__name__", None)


@pytest.mark.parametrize(("model", "path", "cap", "make_"), STRINGS, ids=_id)
def test_a_handed_on_text_is_capped_in_the_schema_and_by_pydantic(model, path, cap, make_):
    node = json_schema_for(model)["properties"]
    for key in path:
        node = node[key]
    assert node["type"] == "string" and node["maxLength"] == cap  # what the CLI's schema check enforces
    model.model_validate(make_(cap))  # exactly at the cap: accepted whole
    with pytest.raises(ValidationError, match=f"at most {cap} characters"):
        model.model_validate(make_(cap + 1))


@pytest.mark.parametrize(("model", "field", "cap", "make_"), LISTS, ids=_id)
def test_a_handed_on_list_is_capped_in_the_schema_and_by_pydantic(model, field, cap, make_):
    node = json_schema_for(model)["properties"][field]
    assert node["type"] == "array" and node["maxItems"] == cap
    model.model_validate(make_(cap))
    with pytest.raises(ValidationError, match=f"at most {cap} items"):
        model.model_validate(make_(cap + 1))


def test_the_caps_reach_the_schema_each_role_is_given_and_nothing_else_is_capped():
    """Every role's --json-schema: the capped fields carry maxLength / maxItems; the reports themselves (a stream's
    section, the final report, a synthesiser's reasons) and the verifiers' outputs are not capped."""

    def caps(node, path=()):
        if isinstance(node, dict):
            if "maxLength" in node or "maxItems" in node:
                yield ".".join(p for p in path if p not in ("properties", "items"))
            for k, v in node.items():
                yield from caps(v, (*path, k))

    got = {name: sorted(set(caps(json_schema_for(ROLES[name].output)))) for name in ROLES}
    plan = ["streams", "streams.leads", "streams.questions"]
    stream = ["open_questions", "red_flags", "summary"]
    case = ["listing_view", "long_term_view", "points", "points.point", "strongest_counterargument", "thesis"]
    for name, role in ROLES.items():
        want = {S.ResearchPlan: plan, S.StreamReport: stream, S.CaseReport: case,
                S.CriticReport: ["gaps", "gaps.task"]}.get(role.output, [])  # fmt: skip
        assert got[name] == want, name


# --------------------------------------------------------------------------- what the caps were set against
def test_the_caps_clear_the_longest_comparable_text_in_the_replay_fixtures():
    """The replay fixtures hold no step outputs (evals.replay exports the steps' timings, the claims and the final
    report), so the capped fields themselves cannot be measured there: if a fixture ever carries outputs, this test
    fails and the real fields must be measured. Until then the caps are held against the closest text an agent
    wrote in those four live runs."""
    statement = bullet = paragraph = 0
    files = sorted((FIX / "eval").glob("*.json"))
    for p in files:
        d = json.loads(p.read_text())
        assert all("output" not in step for step in d["steps"]), f"{p.name} has step outputs: measure them"
        statement = max(statement, *(len(c["statement"] or "") for c in d["claims"]))
        md = d["synthesis"]["report_markdown"]
        bullets = [ln.strip() for ln in md.splitlines() if re.match(r"\s*([-*]|\d+\.)\s", ln)]
        paragraphs = [x for x in re.split(r"\n\s*\n", md) if not x.lstrip().startswith(("|", "#"))]
        bullet, paragraph = max(bullet, *map(len, bullets)), max(paragraph, *map(len, paragraphs))
    assert len(files) == 4 and (statement, bullet, paragraph) == (1016, 463, 2398)
    assert statement * 1.4 < S.HANDOFF_LINE_CHARS and bullet * 3 < S.HANDOFF_LINE_CHARS  # one line
    assert statement * 1.9 < S.HANDOFF_TASK_CHARS and paragraph * 1.6 < S.HANDOFF_VIEW_CHARS
    assert paragraph * 3 < S.HANDOFF_SUMMARY_CHARS  # 8-15 sentences
    # the planner prompts ask for 4-10 questions a stream; no kind has more than seven streams
    for name in ("planner", "stock_planner", "fund_planner", "bond_planner"):
        assert re.search(r"4.10 (specific )?questions", ROLES[name].template_text()), name
    assert S.HANDOFF_LIST_ITEMS >= 3 * 10 and S.PLAN_STREAMS > 7


# --------------------------------------------------------------------------- an exceeded cap is loud, never silent
OVER = {"summary": "s", "section_markdown": "Revenue grew [C1].", "key_findings": [], "claim_ids": [1],
        "red_flags": ["x" * (LINE + 600)]}  # fmt: skip
GAVE_UP = ("run7-risks: error_max_structured_output_retries Failed to provide valid structured output after 5 attempts "
           "\u2014 last StructuredOutput error: Output does not match required schema: /red_flags/0: must NOT have more "
           "than 1500 characters (got 2100)")  # fmt: skip


def _engine(tmp_path, monkeypatch, scenario: str, output: dict | None = None):
    from finresearch.bridge.claude_code import ClaudeCodeEngine
    from finresearch.bridge.limits import LimitTracker
    from finresearch.bridge.router import BridgeRouter
    from finresearch.bridge.types import ModelClass, Tier

    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIX / "fake_claude.py"}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", scenario)
    if output is not None:
        monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps(output))
    eng = ClaudeCodeEngine(Tier.CLAUDE_MAX, claude_bin=str(wrapper), models={m: "sonnet" for m in ModelClass},
                           runs_dir=tmp_path / "runs")  # fmt: skip
    tracker = LimitTracker(tmp_path / "st")
    return BridgeRouter({Tier.CLAUDE_MAX: eng}, [Tier.CLAUDE_MAX], tracker, tmp_path / "l.jsonl")


def _ctx():
    from finresearch.agents.runner import RunContext

    return RunContext(run_id=7, company_slug="acme", company_name="Acme Cables Ltd", nse_symbol="ACME")


async def test_an_over_long_answer_the_engine_let_through_is_refused_whole(tmp_path, monkeypatch):
    """The backstop behind the CLI's own check: Pydantic refuses the output (it is never cut to fit), and the error
    names the field and the cap."""
    from finresearch.agents.runner import RoleOutputInvalid, run_role

    with pytest.raises(RoleOutputInvalid) as e:
        await run_role("financials", _ctx(), router=_engine(tmp_path, monkeypatch, "ok", OVER))
    assert "red_flags.0" in str(e.value) and f"at most {LINE} characters" in str(e.value)


async def test_the_clis_give_up_after_its_retries_keeps_the_reason(tmp_path, monkeypatch):
    """Claude Code checks the answer against --json-schema itself and has the model try again in the same session;
    when that fails five times it ends with error_max_structured_output_retries. The bridge reports a failed task,
    with the CLI's reason (which field, which cap, how long): not a schema-less success, not a silent retry."""
    from finresearch.agents.runner import run_role
    from finresearch.bridge.types import TaskFailed

    with pytest.raises(TaskFailed) as e:
        await run_role("financials", _ctx(), router=_engine(tmp_path, monkeypatch, "structured_retries"))
    assert "error_max_structured_output_retries" in str(e.value)
    assert "/red_flags/0: must NOT have more than 1500 characters (got 2100)" in str(e.value)


class OverCap(FakeRunner):
    """The risks stream's answer breaks a cap, the way `how` says: the CLI gave up, or Pydantic refused it."""

    def __init__(self, run_id, how: str):
        super().__init__(run_id)
        self.how = how

    async def __call__(self, role, ctx, **extra):
        from finresearch.agents.runner import RoleOutputInvalid
        from finresearch.bridge.types import TaskFailed

        if role != "risks":
            return await super().__call__(role, ctx, **extra)
        self.calls.append(role)
        if self.how == "cli":
            raise TaskFailed(GAVE_UP)
        try:
            S.StreamReport.model_validate(OVER)
        except ValidationError as e:
            raise RoleOutputInvalid(f"risks: output failed validation: {e}") from e


@pytest.mark.parametrize(("how", "says"), [("cli", "must NOT have more than 1500 characters (got 2100)"),
                                           ("pydantic", f"at most {LINE} characters")])  # fmt: skip
async def test_a_broken_cap_fails_the_step_and_a_resume_reruns_only_it(company_run, tmp_path, how, says):  # noqa: F811
    """Not retried in place, not paused, nothing cut: the step and the run are failed with the field named, the
    other streams' work is kept, and a resume runs the failed stream again and finishes."""
    runner = OverCap(company_run, how)
    assert await make(company_run, tmp_path, runner).run() == "failed"
    status, resume_after, manifest = run_row(company_run)
    st = steps(company_run)
    assert status == "failed" and resume_after is None and runner.calls.count("risks") == 1
    assert st["stream:risks"] == "failed" and st["stream:financials"] == st["stream:news30"] == "done"
    assert manifest["last_error"]["type"] == "StepFailed" and says in manifest["last_error"]["message"]
    assert "red_flags" in manifest["last_error"]["message"]
    again = FakeRunner(company_run)
    assert await make(company_run, tmp_path, again).run() == "done"
    assert "risks" in again.calls and not {"planner", "financials", "news30"} & set(again.calls)


async def test_a_stored_output_over_a_cap_fails_the_resume_by_name(company_run, tmp_path):  # noqa: F811
    """A finished step's stored output is validated again when a run resumes (orchestrator.base.step). One written
    before the caps and longer than a cap stops the resume with the field named: it is neither cut nor passed on."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep

    assert await make(company_run, tmp_path, OverCap(company_run, "pydantic")).run() == "failed"
    with session_scope() as s:
        plan = s.scalar(select(AgentStep).where(AgentStep.run_id == company_run, AgentStep.key == "planner"))
        good = plan.output
        first = {**good["streams"][0], "questions": ["q" * (LINE + 1)]}
        plan.output = {**good, "streams": [first, *good["streams"][1:]]}
    runner = FakeRunner(company_run)
    assert await make(company_run, tmp_path, runner).run() == "failed"
    _, _, manifest = run_row(company_run)
    assert manifest["last_error"]["type"] == "ValidationError" and runner.calls == []
    assert "streams.0.questions.0" in manifest["last_error"]["message"]
    assert f"at most {LINE} characters" in manifest["last_error"]["message"]
    with session_scope() as s:  # the stored text is untouched
        plan = s.scalar(select(AgentStep).where(AgentStep.run_id == company_run, AgentStep.key == "planner"))
        assert plan.status == "done" and len(plan.output["streams"][0]["questions"][0]) == LINE + 1
        plan.output = good
    assert await make(company_run, tmp_path, FakeRunner(company_run)).run() == "done"

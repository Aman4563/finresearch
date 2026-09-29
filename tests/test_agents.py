"""Agent roles: prompt rendering, least-privilege tools, schemas, workspaces, validated runs (fake CLI)."""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path

import jsonschema
import pytest

from finresearch.agents.roles import (
    BOND_STREAMS,
    FUND_STREAMS,
    LEDGER_WRITE,
    MARKET,
    ROLES,
    STOCK_STREAMS,
    STREAMS,
    WEB,
)
from finresearch.agents.runner import RoleOutputInvalid, RunContext, build_task, render, run_role
from finresearch.agents.schemas import StreamReport, json_schema_for
from finresearch.bridge.claude_code import ClaudeCodeEngine
from finresearch.bridge.limits import LimitTracker
from finresearch.bridge.router import BridgeRouter
from finresearch.bridge.types import Capability, ModelClass, Tier

FIX = Path(__file__).parent / "fixtures"
CTX = RunContext(run_id=7, company_slug="orient-cables", company_name="Orient Cables (India) Limited",
                 nse_symbol="ORIENTCABL", documents=[{"document_id": 1, "kind": "RHP", "title": "RHP",
                 "pages": 491, "sections": 28}])  # fmt: skip


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from finresearch import config

    monkeypatch.setenv("FINRESEARCH_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("FINRESEARCH_STATE_DIR", str(tmp_path / "state"))
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.mark.parametrize("name", sorted(ROLES))
def test_every_role_renders_without_unfilled_placeholders(name):
    system, prompt = render(ROLES[name], CTX, target_stream="financials", claims="[]", stream_reports="-",
                            bull="-", bear="-", draft="-", revision="none")  # fmt: skip
    leftovers = set(re.findall(r"\{([a-z_]+)\}", system + prompt)) - {"focus"}
    assert not leftovers, f"{name} left placeholders {leftovers}"
    assert "run_id 7" in system or "run_id=7" in system or "run 7" in system.lower() or name in {"planner"}
    assert "Orient Cables" in prompt


def test_least_privilege_tools():
    for name, role in ROLES.items():
        has_web = bool(set(WEB) & set(role.tools))
        assert has_web == role.needs_web, f"{name}: web tools must match needs_web"
        writes = bool(set(LEDGER_WRITE[:1]) & set(role.tools))
        assert writes == role.writes_claims, f"{name}: only research streams may save claims"
        assert "Bash" not in role.tools and "Write" not in role.tools and "Edit" not in role.tools
    assert set(STREAMS) | set(STOCK_STREAMS) | set(FUND_STREAMS) | set(BOND_STREAMS) == {
        n for n, r in ROLES.items() if r.writes_claims
    }
    assert not ROLES["financials"].needs_web and ROLES["news30"].needs_web
    # stock streams read NSE equity data; only the IPO roles get the IPO market tools
    assert not set(MARKET) & {t for n in STOCK_STREAMS for t in ROLES[n].tools}
    assert not ROLES["stock_technical"].needs_web and ROLES["stock_news"].needs_web


@pytest.mark.parametrize("name", sorted(ROLES))
def test_output_schemas_are_self_contained(name):
    schema = json_schema_for(ROLES[name].output)
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    jsonschema.Draft202012Validator.check_schema(schema)


def test_task_is_claude_only_with_web_capability_and_sandboxed_skills():
    t = build_task("news30", CTX)
    assert t.allow_degraded is False and Capability.WEB in t.capabilities
    assert t.model_class is ModelClass.STANDARD and t.mcp_config and t.mcp_config.exists()
    skills = sorted(p.name for p in (t.run_dir / ".claude" / "skills").iterdir())
    assert skills == sorted(ROLES["news30"].skills)
    for sk in skills:
        head = (t.run_dir / ".claude" / "skills" / sk / "SKILL.md").read_text().split("---")[1]
        assert f"name: {sk}" in head and "description:" in head
    assert Capability.WEB not in build_task("financials", CTX).capabilities


def _fake_router(tmp_path, monkeypatch, output: dict) -> BridgeRouter:
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIX / "fake_claude.py"}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps(output))
    eng = ClaudeCodeEngine(Tier.CLAUDE_MAX, claude_bin=str(wrapper),
                           models={m: "sonnet" for m in ModelClass}, runs_dir=tmp_path / "runs")  # fmt: skip
    return BridgeRouter(
        {Tier.CLAUDE_MAX: eng}, [Tier.CLAUDE_MAX], LimitTracker(tmp_path / "st"), tmp_path / "l.jsonl"
    )


async def test_run_role_validates_output(tmp_path, monkeypatch):
    good = StreamReport(summary="s", section_markdown="Revenue grew [C1].", claim_ids=[1],
                        key_findings=[{"finding": "f", "impact": "positive", "claim_ids": [1]}]).model_dump()  # fmt: skip
    parsed, res = await run_role("financials", CTX, router=_fake_router(tmp_path, monkeypatch, good))
    assert isinstance(parsed, StreamReport) and parsed.claim_ids == [1] and res.tier is Tier.CLAUDE_MAX
    with pytest.raises(RoleOutputInvalid):
        await run_role("financials", CTX, router=_fake_router(tmp_path, monkeypatch, {"summary": "only"}))

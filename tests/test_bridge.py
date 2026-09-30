"""Offline tests for the Claude Bridge: parsing, limit handling, failover, local-tier guarantees."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from finresearch.bridge.claude_code import ClaudeCodeEngine
from finresearch.bridge.limits import LimitTracker
from finresearch.bridge.ollama_engine import OllamaEngine
from finresearch.bridge.router import AllTiersFailed, BridgeRouter
from finresearch.bridge.types import (
    AgentTask,
    Capability,
    CapabilityMismatch,
    EngineUnavailable,
    LimitReached,
    ModelClass,
    SchemaViolation,
    Tier,
    TransientError,
)

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
MODELS = {ModelClass.DEEP: "opus", ModelClass.STANDARD: "sonnet", ModelClass.FAST: "haiku"}
LOCAL = {ModelClass.DEEP: "qwen3.5:9b", ModelClass.STANDARD: "qwen3.5:9b", ModelClass.FAST: "qwen3.5:4b"}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """An executable wrapper so the engine can exec it like the real `claude`."""
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURES / "fake_claude.py"}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")

    def make(scenario: str = "ok") -> ClaudeCodeEngine:
        monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", scenario)
        return ClaudeCodeEngine(
            Tier.CLAUDE_MAX, claude_bin=str(wrapper), models=MODELS, runs_dir=tmp_path / "runs"
        )

    return make


def ollama_transport(handler_outputs: list[str], *, loaded=("qwen3.5:9b",)) -> httpx.MockTransport:
    outputs = list(handler_outputs)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            names = ["qwen3.5:9b", "qwen3.5:4b", "qwen3-embedding:0.6b", "glm-ocr:latest"]
            return httpx.Response(200, json={"models": [{"name": n} for n in names]})
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": n, "size": 6 * 1024**3} for n in loaded]})
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.34.3"})
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            calls.append(body)
            return httpx.Response(200, json={"message": {"content": outputs.pop(0)}, "prompt_eval_count": 50,
                                              "eval_count": 10, "done_reason": "stop"})  # fmt: skip
        return httpx.Response(404)

    t = httpx.MockTransport(handler)
    t.calls = calls  # type: ignore[attr-defined]
    return t


def local_engine(transport, free_gb=10.0) -> OllamaEngine:
    return OllamaEngine(
        base_url="http://ollama.test", models=LOCAL, low_memory_model="qwen3.5:4b", min_free_gb_for_large=7.0,
        embed_model="qwen3-embedding:0.6b", ocr_model="glm-ocr", memory_probe=lambda: free_gb,
        transport=transport,
    )  # fmt: skip


def task(**kw) -> AgentTask:
    base = {"name": "t", "prompt": "hello", "json_schema": SCHEMA, "model_class": ModelClass.FAST}
    return AgentTask(**(base | kw))


# ------------------------------------------------------------------ Claude Code engine
async def test_claude_success_strips_api_key_and_parses(fake_claude):
    res = await fake_claude("ok").run(task())
    assert res.ok and res.structured_output["answer"] == "ok"
    assert res.structured_output["leak"] is False, "API key leaked into the Max tier"
    assert res.rate_limit.five_hour_utilization == 0.40
    assert res.input_tokens == 110 and res.cost_usd_estimate == pytest.approx(0.0123)
    assert res.transcript_path.exists()


def test_command_disables_tools_and_user_settings(fake_claude):
    cmd = fake_claude("ok").build_command(task())
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert cmd[cmd.index("--model") + 1] == "haiku"
    web = fake_claude("ok").build_command(task(allowed_tools=["WebSearch", "WebFetch"]))
    assert web[web.index("--tools") + 1] == "WebSearch,WebFetch"


async def test_claude_limit_detected_with_reset(fake_claude):
    with pytest.raises(LimitReached) as ei:
        await fake_claude("limit").run(task())
    assert ei.value.resets_at == 4102444800


async def test_claude_overload_is_transient(fake_claude):
    with pytest.raises(TransientError):
        await fake_claude("overloaded").run(task())


async def test_claude_auth_failure_is_unavailable(fake_claude):
    with pytest.raises(EngineUnavailable):
        await fake_claude("auth_fail").run(task())


async def test_claude_crash_without_result_is_transient(fake_claude):
    with pytest.raises(TransientError):
        await fake_claude("crash").run(task())


async def test_claude_missing_structured_output(fake_claude):
    with pytest.raises(SchemaViolation):
        await fake_claude("no_structured").run(task())


async def test_health_requires_subscription_login(fake_claude):
    assert (await fake_claude("ok").health())["auth_method"] == "claude.ai"
    with pytest.raises(EngineUnavailable):
        await fake_claude("logged_out").health()


# ------------------------------------------------------------------ Local engine
async def test_local_structured_ok():
    t = ollama_transport(['{"answer": "local"}'])
    res = await local_engine(t).run(task())
    assert res.structured_output == {"answer": "local"} and res.model == "qwen3.5:4b"
    body = t.calls[0]
    assert body["think"] is False and body["format"] == SCHEMA and body["options"]["temperature"] == 0


async def test_local_repairs_invalid_json():
    t = ollama_transport(["not json", '```json\n{"answer": "fixed"}\n```'])
    res = await local_engine(t).run(task())
    assert res.structured_output == {"answer": "fixed"} and any("repair" in w for w in res.warnings)


async def test_local_gives_up_after_repairs():
    t = ollama_transport(["{}", "{}", "{}"])
    with pytest.raises(SchemaViolation):
        await local_engine(t).run(task())


async def test_local_refuses_web_and_long_prompts():
    eng = local_engine(ollama_transport([]))
    with pytest.raises(CapabilityMismatch):
        await eng.run(task(capabilities={Capability.WEB}))
    with pytest.raises(CapabilityMismatch):
        await eng.run(task(prompt="x" * 100_000))


async def test_local_downgrades_9b_when_memory_tight():
    t = ollama_transport(['{"answer": "small"}'], loaded=())
    res = await local_engine(t, free_gb=3.0).run(task(model_class=ModelClass.DEEP))
    assert res.model == "qwen3.5:4b" and any("reclaimable" in w for w in res.warnings)
    t2 = ollama_transport(['{"answer": "big"}'], loaded=())
    res2 = await local_engine(t2, free_gb=9.0).run(task(model_class=ModelClass.DEEP))
    assert res2.model == "qwen3.5:9b"


# ------------------------------------------------------------------ Router / failover
def router(tmp_path, claude_engine, local=None) -> BridgeRouter:
    engines = {Tier.CLAUDE_MAX: claude_engine}
    if local:
        engines[Tier.LOCAL] = local
    tracker = LimitTracker(tmp_path / "state", breaker_failures=2, breaker_cooldown_s=60)
    return BridgeRouter(
        engines, [Tier.CLAUDE_MAX, Tier.CLAUDE_API, Tier.LOCAL], tracker, tmp_path / "ledger.jsonl"
    )


async def test_router_prefers_max(tmp_path, fake_claude):
    r = router(tmp_path, fake_claude("ok"), local_engine(ollama_transport([])))
    res = await r.run(task())
    assert res.tier is Tier.CLAUDE_MAX and not res.degraded
    assert r.tracker.snapshot(Tier.CLAUDE_MAX).five_hour_utilization == 0.40


async def test_router_fails_over_on_limit_and_then_skips_max(tmp_path, fake_claude):
    r = router(
        tmp_path, fake_claude("limit"), local_engine(ollama_transport(['{"answer":"l1"}', '{"answer":"l2"}']))
    )
    res = await r.run(task())
    assert res.tier is Tier.LOCAL and res.degraded and "claude_max:limit" in res.attempts
    res2 = await r.run(task())  # Max should now be skipped without being called
    assert res2.tier is Tier.LOCAL and any("claude_max:skipped" in a for a in res2.attempts)
    lines = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert [x["outcome"] for x in lines] == ["limit", "ok", "ok"]


async def test_router_preflight_ceiling_blocks_max(tmp_path, fake_claude):
    r = router(tmp_path, fake_claude("near_ceiling"), local_engine(ollama_transport(['{"answer":"l"}'])))
    first = await r.run(task())  # succeeds on Max but records 95% utilisation
    assert first.tier is Tier.CLAUDE_MAX
    second = await r.run(task())
    assert second.tier is Tier.LOCAL and any("pre-flight" in a for a in second.attempts)


async def test_router_web_task_never_faked_locally(tmp_path, fake_claude):
    r = router(tmp_path, fake_claude("limit"), local_engine(ollama_transport([])))
    with pytest.raises(AllTiersFailed) as ei:
        await r.run(task(capabilities={Capability.WEB}, allowed_tools=["WebSearch"]))
    assert any("CapabilityMismatch" in a for a in ei.value.attempts)


async def test_router_respects_allow_degraded_false(tmp_path, fake_claude):
    r = router(tmp_path, fake_claude("limit"), local_engine(ollama_transport(['{"answer":"x"}'])))
    with pytest.raises(AllTiersFailed):
        await r.run(task(allow_degraded=False))


async def test_circuit_breaker_opens_after_transient_failures(tmp_path, fake_claude):
    r = router(tmp_path, fake_claude("overloaded"), local_engine(ollama_transport(['{"answer":"a"}'] * 3)))
    for _ in range(2):
        assert (await r.run(task())).tier is Tier.LOCAL
    ok, why = r.tracker.availability(Tier.CLAUDE_MAX)
    assert not ok and "circuit open" in why


def test_env_never_contains_api_key_for_max(fake_claude):
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "x"
    try:
        env = fake_claude("ok")._env()
        assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    finally:
        del os.environ["ANTHROPIC_AUTH_TOKEN"]


# ---------------------------------------------------------------- transient CLI failures (run 13, 2026-09-30)
# "<fake CLI scenario> -> <expected kind>"
@pytest.mark.parametrize(
    "case",
    [
        "sleep -> sleep",
        "login_refresh -> oauth",
        "enotfound -> network",
        "conn_reset -> network",
        "cut_off -> sleep",
    ],
)
async def test_machine_and_network_failures_are_transient(fake_claude, case):
    """The CLI reports these as an is_error result with subtype "success" (or exits with ECONNRESET on stderr);
    the OAuth collision used to be a TaskFailed that crashed the worker."""
    scenario, kind = case.split(" -> ")
    with pytest.raises(TransientError) as ei:
        await fake_claude(scenario).run(task(json_schema=SCHEMA))
    assert ei.value.kind == kind
    assert " success " not in str(ei.value)  # the misleading subtype is not part of the reason


async def test_a_real_task_error_is_still_task_failed(fake_claude):
    from finresearch.bridge.types import TaskFailed

    with pytest.raises(TaskFailed):
        await fake_claude("denied").run(task())


def test_transient_kind_classifies_cli_messages():
    from finresearch.bridge.types import transient_kind

    assert transient_kind("API Error: Your computer went to sleep mid-response.") == "sleep"
    assert (
        transient_kind("Failed to refresh OAuth token: another Claude Code process is refreshing it")
        == "oauth"
    )
    assert transient_kind("socket hang up") == transient_kind("connect ETIMEDOUT 1.2.3.4:443") == "network"
    assert transient_kind("API Error: 529 overloaded_error") == "overloaded"
    assert transient_kind(None, "", "Error: read ECONNRESET") == "network"
    for text in ("Claude AI usage limit reached|1790000000", "Invalid API key", "Permission to use Bash denied",
                 "output failed validation", None):  # fmt: skip
        assert transient_kind(text) is None


async def test_local_transient_failures_do_not_open_the_circuit(tmp_path, fake_claude):
    """A Mac that slept or a login-refresh collision says nothing about the service: no breaker, and the router
    reports the error as transient so the pipeline retries or pauses instead of failing the run."""
    r = router(tmp_path, fake_claude("login_refresh"))
    for _ in range(3):
        with pytest.raises(AllTiersFailed) as ei:
            await r.run(task(allow_degraded=False))
        assert ei.value.transient is not None and ei.value.transient.kind == "oauth" and not ei.value.limited
    assert r.tracker.availability(Tier.CLAUDE_MAX)[0]


def test_all_tiers_failed_tells_limits_from_transient_errors():
    from finresearch.bridge.types import EngineUnavailable as EU

    t = TransientError("x", kind="network")
    assert (
        AllTiersFailed("t", ["claude_max:transient(x)", "local:skipped(allow_degraded=False)"], [t]).transient
        is t
    )
    assert AllTiersFailed("t", ["claude_max:limit", "local:skipped(allow_degraded=False)"], []).limited
    assert AllTiersFailed("t", ["claude_max:skipped(circuit open after 3 failures: x)"], []).limited
    assert not AllTiersFailed("t", ["local:skipped(allow_degraded=False)"], []).limited
    mixed = AllTiersFailed("t", ["claude_max:transient(x)", "local:EngineUnavailable(down)"], [t, EU("down")])
    assert mixed.transient is None


async def test_a_silent_stream_is_given_up_as_transient(fake_claude):
    with pytest.raises(TransientError) as ei:
        await fake_claude("silent").run(task(idle_timeout_s=1))
    assert ei.value.kind == "idle" and "printed nothing" in str(ei.value)

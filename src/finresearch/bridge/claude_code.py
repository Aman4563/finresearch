"""Tier 1/2 engine: drives the OFFICIAL Claude Code CLI (`claude -p`) as a subprocess.

* Tier.CLAUDE_MAX -> uses the logged-in claude.ai (Max) account. Any ANTHROPIC_API_KEY / auth-token env
  vars are stripped so the subscription is always what gets used.
* Tier.CLAUDE_API -> same CLI, but ANTHROPIC_API_KEY is injected (only when explicitly enabled).

Nothing here extracts or re-exposes OAuth credentials; the CLI owns authentication.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

from finresearch.bridge.types import (
    AgentResult,
    AgentTask,
    Capability,
    EngineUnavailable,
    LimitReached,
    RateLimitSnapshot,
    SchemaViolation,
    TaskFailed,
    Tier,
    TransientError,
    transient_kind,
)
from finresearch.fetch_guard import DENY_RULES, write_sandbox_settings

# Env vars that would silently switch the CLI away from the subscription login.
_AUTH_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_PROFILE",
)
_LIMIT_TEXT = re.compile(
    r"(usage limit|limit reached|rate limit|out of (extra )?usage|weekly limit|5-hour limit)", re.I
)
_LIMIT_EPOCH = re.compile(r"limit reached\|(\d{9,11})", re.I)
_LIMIT_ERRORS = {"rate_limit", "billing_error"}
_UNAVAILABLE_ERRORS = {"authentication_failed", "oauth_org_not_allowed", "account_on_hold", "model_not_found"}
_TRANSIENT_ERRORS = {"overloaded", "server_error", "unknown"}


def _snapshot_from_event(info: dict[str, Any]) -> RateLimitSnapshot:
    windows = info.get("unifiedWindows") or {}
    five = windows.get("five_hour") or {}
    seven = windows.get("seven_day") or {}
    return RateLimitSnapshot(
        status=info.get("status"),
        rate_limit_type=info.get("rateLimitType"),
        resets_at=info.get("resetsAt"),
        five_hour_utilization=five.get("utilization"),
        five_hour_resets_at=five.get("resetsAt"),
        seven_day_utilization=seven.get("utilization"),
        seven_day_resets_at=seven.get("resetsAt"),
        overage_status=info.get("overageStatus"),
        observed_at=time.time(),
    )


class ClaudeCodeEngine:
    def __init__(
        self,
        tier: Tier,
        *,
        claude_bin: str = "claude",
        models: dict,
        api_key: str | None = None,
        runs_dir: Path,
    ):
        if tier not in (Tier.CLAUDE_MAX, Tier.CLAUDE_API):
            raise ValueError(tier)
        if tier is Tier.CLAUDE_API and not api_key:
            raise EngineUnavailable("Claude API tier requires ANTHROPIC_API_KEY")
        self.tier = tier
        self.claude_bin = claude_bin
        self.models = models
        self._api_key = api_key
        self.runs_dir = Path(runs_dir)

    # ------------------------------------------------------------ health
    async def health(self) -> dict[str, Any]:
        """Check the CLI exists and (for the Max tier) that a claude.ai subscription login is active."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.claude_bin, "auth", "status",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self._env(),
            )  # fmt: skip
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (FileNotFoundError, TimeoutError) as e:
            raise EngineUnavailable(f"claude CLI not runnable: {e}") from e
        try:
            status = json.loads(out.decode() or "{}")
        except json.JSONDecodeError:
            status = {"raw": out.decode()[:500], "stderr": err.decode()[:500]}
        if self.tier is Tier.CLAUDE_MAX and (
            not status.get("loggedIn") or status.get("authMethod") != "claude.ai"
        ):
            raise EngineUnavailable(
                "Claude Code is not logged in with a claude.ai subscription (run `claude` then /login)",
                detail={"auth": {k: status.get(k) for k in ("loggedIn", "authMethod")}},
            )
        return {
            "tier": self.tier.value,
            "logged_in": status.get("loggedIn"),
            "auth_method": status.get("authMethod"),
            "subscription": status.get("subscriptionType") or status.get("plan"),
        }

    # ------------------------------------------------------------ run
    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _AUTH_OVERRIDES}
        if self.tier is Tier.CLAUDE_API and self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        env.setdefault("CLAUDE_CODE_FORWARD_SUBAGENT_TEXT", "1")
        return env

    def build_command(self, task: AgentTask) -> list[str]:
        model = self.models[task.model_class]
        cmd = [
            self.claude_bin, "-p",
            "--output-format", "stream-json", "--verbose",
            "--model", model,
            "--permission-mode", "dontAsk",
            "--permission-prompts", "none",
            "--max-turns", str(task.max_turns),
            "--setting-sources", "project,local",  # never load the user's personal hooks/plugins
            "--strict-mcp-config",  # only the MCP servers we pass explicitly
        ]  # fmt: skip
        builtin = [t for t in task.allowed_tools if not t.startswith("mcp__")]
        # built-in tools must be listed in --tools (availability); MCP tools only need permission
        cmd += ["--tools", ",".join(builtin)]
        if task.allowed_tools:
            cmd += ["--allowedTools", ",".join(task.allowed_tools)]
        if "WebFetch" in builtin:  # never this machine or the local network (finresearch.fetch_guard, #246)
            cmd += ["--disallowedTools", ",".join(DENY_RULES)]
        if task.json_schema is not None:
            cmd += ["--json-schema", json.dumps(task.json_schema, separators=(",", ":"))]
        if task.effort:
            cmd += ["--effort", task.effort]
        if task.system_prompt:
            cmd += ["--append-system-prompt", task.system_prompt]
        if task.mcp_config:
            cmd += ["--mcp-config", str(task.mcp_config)]
        if task.agents_json:
            cmd += ["--agents", str(task.agents_json)]
        if task.resume_session_id:
            cmd += ["--resume", task.resume_session_id]
        return cmd

    async def run(self, task: AgentTask) -> AgentResult:
        if Capability.WEB in task.capabilities and not {"WebSearch", "WebFetch"} & set(task.allowed_tools):
            raise TaskFailed(f"task {task.name} needs web but allows neither WebSearch nor WebFetch")

        run_dir = Path(task.run_dir or self.runs_dir / "adhoc")
        run_dir.mkdir(parents=True, exist_ok=True)
        transcripts = run_dir / "transcripts"
        transcripts.mkdir(exist_ok=True)
        transcript_path = transcripts / f"{task.name}-{self.tier.value}-{int(time.time() * 1000)}.jsonl"

        cmd = self.build_command(task)
        # the sandbox's project settings: WebFetch deny rules and the private-address hook (finresearch.fetch_guard)
        write_sandbox_settings(run_dir)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=run_dir, env=self._env(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=64 * 1024 * 1024,
            )  # fmt: skip
        except FileNotFoundError as e:
            raise EngineUnavailable(f"claude CLI not found: {self.claude_bin}") from e

        state = _StreamState()
        stderr_task = asyncio.create_task(proc.stderr.read()) if proc.stderr else None
        try:
            await asyncio.wait_for(
                self._pump(proc, task.prompt, transcript_path, state, task.idle_timeout_s),
                timeout=task.timeout_s,
            )
        except _Idle as e:
            await _terminate(proc)
            if stderr_task:
                stderr_task.cancel()
            raise TransientError(
                f"{task.name}: the CLI printed nothing for {task.idle_timeout_s:.0f}s (stream went silent)",
                kind="idle",
            ) from e
        except TimeoutError as e:
            await _terminate(proc)
            if stderr_task:
                stderr_task.cancel()
            raise TransientError(f"{task.name}: timed out after {task.timeout_s:.0f}s", kind="timeout") from e
        rc = await proc.wait()
        stderr = (await stderr_task).decode(errors="replace") if stderr_task else ""
        duration = time.monotonic() - started
        return self._interpret(task, state, rc, stderr, duration, transcript_path)

    async def _pump(self, proc, prompt: str, transcript_path: Path, state: _StreamState,
                    idle_s: float | None = None) -> None:  # fmt: skip
        assert proc.stdin and proc.stdout
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        with open(transcript_path, "w") as tf:
            while True:
                try:  # a stream that goes silent (a connection left hanging after sleep) is given up on
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=idle_s)
                except TimeoutError as e:
                    raise _Idle from e
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                tf.write(line + "\n")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    state.noise.append(line[:300])
                    continue
                state.consume(event)

    def _interpret(self, task, state, rc, stderr, duration, transcript_path) -> AgentResult:
        res = state.result
        snap = state.snapshot
        text = (res or {}).get("result") or ""
        errors_seen = set(state.retry_errors)

        # ---- limit detection (several independent signals)
        failed = res is None or bool(res.get("is_error"))
        limit_hit = failed and (
            (snap is not None and snap.status == "rejected")
            or bool(errors_seen & _LIMIT_ERRORS)
            or bool(_LIMIT_TEXT.search(text))
            or bool(_LIMIT_TEXT.search(stderr))
        )
        if limit_hit:
            m = _LIMIT_EPOCH.search(text)
            resets = int(m.group(1)) if m else (snap.resets_at if snap else None)
            raise LimitReached(
                f"{self.tier.value}: usage/rate limit reached", resets_at=resets,
                detail={"text": text[:300], "errors": sorted(errors_seen)},
            )  # fmt: skip
        if errors_seen & _UNAVAILABLE_ERRORS:
            raise EngineUnavailable(f"{self.tier.value}: {sorted(errors_seen & _UNAVAILABLE_ERRORS)}")
        if res is None:
            detail = {"rc": rc, "stderr": stderr[-800:], "noise": state.noise[-3:]}
            kind = transient_kind(stderr, *state.noise[-3:]) or "crash"
            if kind == "crash" and ("not logged in" in stderr.lower() or "login" in stderr.lower()):
                raise EngineUnavailable(f"{self.tier.value}: CLI reports no login", detail=detail)
            raise TransientError(
                f"{task.name}: CLI exited ({rc}) without a result event", kind=kind, detail=detail
            )
        # the CLI reports some failures of the machine or network (the Mac slept mid-response, two processes
        # refreshed the login at once, no connection) as an is_error result whose subtype is still "success"
        subtype = res.get("subtype", "")
        why = f"{task.name}: {text[:300]}" if subtype == "success" else f"{task.name}: {subtype} {text[:300]}"
        if res.get("is_error"):
            kind = transient_kind(text)
            if kind is None and (
                errors_seen & _TRANSIENT_ERRORS or res.get("api_error_status") in (500, 502, 503, 529)
            ):
                kind = "overloaded"
            if kind:
                raise TransientError(
                    why, kind=kind, detail={"subtype": subtype, "errors": sorted(errors_seen)}
                )
            raise TaskFailed(why, detail={"denials": state.denials})

        structured = res.get("structured_output")
        if task.json_schema is not None and structured is None:
            if kind := transient_kind(text):  # a cut-off answer is not a schema problem: run the step again
                raise TransientError(why, kind=kind, detail={"subtype": subtype})
            raise SchemaViolation(f"{task.name}: no structured_output returned", detail={"text": text[:500]})

        usage = res.get("usage") or {}
        model_used = next(iter(res.get("modelUsage") or {}), self.models[task.model_class])
        warnings = []
        if state.denials:
            warnings.append(f"permission denials: {state.denials[:5]}")
        return AgentResult(
            task_name=task.name, tier=self.tier, model=model_used, ok=True,
            structured_output=structured, text=text, session_id=res.get("session_id"),
            cost_usd_estimate=float(res.get("total_cost_usd") or 0.0),
            input_tokens=int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            duration_s=duration, num_turns=int(res.get("num_turns") or 0),
            rate_limit=snap, warnings=warnings, transcript_path=transcript_path,
        )  # fmt: skip


class _Idle(Exception):
    """No output from the CLI for the task's idle_timeout_s."""


class _StreamState:
    def __init__(self):
        self.result: dict[str, Any] | None = None
        self.snapshot: RateLimitSnapshot | None = None
        self.retry_errors: list[str] = []
        self.denials: list[Any] = []
        self.noise: list[str] = []

    def consume(self, ev: dict[str, Any]) -> None:
        etype, sub = ev.get("type"), ev.get("subtype")
        if etype == "rate_limit_event" and isinstance(ev.get("rate_limit_info"), dict):
            self.snapshot = _snapshot_from_event(ev["rate_limit_info"])
        elif etype == "system" and sub == "api_retry":
            if ev.get("error"):
                self.retry_errors.append(ev["error"])
        elif etype == "system" and sub == "permission_denied":
            self.denials.append(ev.get("tool_name") or ev.get("tool") or "unknown")
        elif etype == "result":
            self.result = ev
            for d in ev.get("permission_denials") or []:
                self.denials.append(d.get("tool_name") if isinstance(d, dict) else d)


async def _terminate(proc) -> None:
    """SIGINT first so Claude Code can close the turn cleanly, then SIGKILL."""
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()

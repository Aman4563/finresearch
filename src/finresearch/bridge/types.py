"""Core types shared by every engine behind the Claude Bridge."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Tier(StrEnum):
    """Engine tiers, in default priority order."""

    CLAUDE_MAX = "claude_max"  # official Claude Code CLI on the logged-in Max subscription
    CLAUDE_API = "claude_api"  # same CLI, billed to ANTHROPIC_API_KEY (disabled unless a key is configured)
    LOCAL = "local"  # Ollama models on this Mac (degraded mode)


class ModelClass(StrEnum):
    """Logical model classes; each tier maps them to a concrete model."""

    DEEP = "deep"  # synthesis, verification, bull/bear (Opus on Claude, qwen3.5:9b locally)
    STANDARD = "standard"  # research streams, extraction (Sonnet / qwen3.5:9b)
    FAST = "fast"  # triage, classification (Haiku / qwen3.5:4b)


class Capability(StrEnum):
    """What a task needs from an engine. Local models cannot browse or run tools."""

    WEB = "web"  # WebSearch / WebFetch
    TOOLS = "tools"  # file / bash / MCP tools
    LONG_CONTEXT = "long_context"  # > ~24k tokens of input


class AgentTask(BaseModel):
    """One unit of LLM work routed through the bridge."""

    name: str = Field(description="Stable task/agent name, used for logs and ledgers")
    prompt: str
    system_prompt: str | None = None
    json_schema: dict[str, Any] | None = None
    model_class: ModelClass = ModelClass.STANDARD
    effort: str | None = Field(default=None, description="low|medium|high|xhigh|max (Claude tiers only)")
    capabilities: set[Capability] = Field(default_factory=set)
    allowed_tools: list[str] = Field(
        default_factory=list, description="Claude Code tool names; [] = no tools"
    )
    mcp_config: Path | None = None
    agents_json: Path | None = Field(default=None, description="Claude Code --agents file for subagents")
    max_turns: int = 20
    timeout_s: float = 1800
    idle_timeout_s: float | None = Field(
        default=900,
        description="Give up (transient) when the CLI prints nothing for this long, e.g. a stream left hanging "
        "after the Mac slept; None disables it",
    )
    run_dir: Path | None = Field(default=None, description="Working directory / sandbox for the run")
    resume_session_id: str | None = None
    allow_degraded: bool = Field(
        default=True, description="If False, the task fails rather than falling back to the local tier"
    )


class RateLimitSnapshot(BaseModel):
    """Subscription window utilisation, as reported by Claude Code `rate_limit_event`."""

    status: str | None = None  # allowed | allowed_warning | rejected
    rate_limit_type: str | None = None
    resets_at: int | None = None  # epoch seconds
    five_hour_utilization: float | None = None
    five_hour_resets_at: int | None = None
    seven_day_utilization: float | None = None
    seven_day_resets_at: int | None = None
    overage_status: str | None = None
    observed_at: float = 0.0


class AgentResult(BaseModel):
    task_name: str
    tier: Tier
    model: str
    ok: bool
    structured_output: Any | None = None
    text: str = ""
    session_id: str | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    cost_usd_estimate: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    num_turns: int = 0
    rate_limit: RateLimitSnapshot | None = None
    warnings: list[str] = Field(default_factory=list)
    transcript_path: Path | None = None
    attempts: list[str] = Field(default_factory=list, description="Tier attempts, e.g. 'claude_max:limit'")


# ---------------------------------------------------------------- errors
class EngineError(Exception):
    """Base class. `retryable_elsewhere` tells the router whether to try the next tier."""

    retryable_elsewhere: bool = True

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.detail = detail or {}


class EngineUnavailable(EngineError):
    """Engine not installed, not logged in, server down, model missing."""


class LimitReached(EngineError):
    """Subscription window exhausted or API rate/billing limit hit."""

    def __init__(self, message: str, *, resets_at: int | None = None, detail: dict[str, Any] | None = None):
        super().__init__(message, detail=detail)
        self.resets_at = resets_at


class TransientError(EngineError):
    """Overload, timeout, network blip — may succeed on retry or another tier.

    `kind` says what went wrong (see `transient_kind`); the pipeline picks its retry delay from it."""

    def __init__(self, message: str, *, kind: str = "other", detail: dict[str, Any] | None = None):
        super().__init__(message, detail=detail)
        self.kind = kind


# Failures of the machine or the network rather than of the task, as the Claude CLI reports them in its result
# text or on stderr. Each may succeed if the same step is simply run again a little later.
_TRANSIENT_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    # the Mac slept while a request was streaming: "API Error: Your computer went to sleep mid-response. ..."
    "sleep": (
        r"went to sleep",
        r"mid-response",
        r"response (above )?may be incomplete",
        r"incomplete response",
        r"response was (cut off|truncated|interrupted)",
    ),
    # two Claude Code processes refreshing the login at once: "Failed to refresh OAuth token: another ..."
    "oauth": (
        r"refresh(ing)? (the )?oauth token",
        r"oauth token refresh",
        r"is refreshing it",
        r"exited mid-refresh",
    ),
    # "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)", resets, timeouts
    "network": (
        r"can.?t reach the api server",
        r"\bENOTFOUND\b",
        r"\bECONNRESET\b",
        r"\bECONNREFUSED\b",
        r"\bETIMEDOUT\b",
        r"\bEPIPE\b",
        r"\bEAI_AGAIN\b",
        r"\bENETUNREACH\b",
        r"\bEHOSTUNREACH\b",
        r"socket hang ?up",
        r"connection (was )?(reset|refused|closed|error|timed out)",
        r"network (error|is unreachable)",
        r"fetch failed",
        r"request timed out",
        r"stream (closed|ended) unexpectedly",
    ),
    "overloaded": (
        r"\boverloaded",
        r"internal server error",
        r"bad gateway",
        r"service unavailable",
        r"gateway time-?out",
        r"api error:? *(500|502|503|504|529)\b",
    ),
}
_TRANSIENT_PATTERNS = {k: re.compile("|".join(v), re.I) for k, v in _TRANSIENT_ALTERNATIVES.items()}


def transient_kind(*texts: str | None) -> str | None:
    """The kind of transient failure a CLI message describes ("sleep", "oauth", "network", "overloaded"), or None."""
    for text in texts:
        if not text:
            continue
        for kind, pat in _TRANSIENT_PATTERNS.items():
            if pat.search(text):
                return kind
    return None


class SchemaViolation(EngineError):
    """Output did not satisfy the task's JSON schema after repair attempts."""


class CapabilityMismatch(EngineError):
    """The engine cannot provide what the task requires (e.g. web access on the local tier)."""


class TaskFailed(EngineError):
    """Non-retryable failure of the task itself (bad prompt/schema, permission denied, etc.)."""

    retryable_elsewhere = False

"""Claude Bridge: one interface over Claude Code (Max subscription), Claude API (optional) and local Ollama."""

from __future__ import annotations

import contextlib

from finresearch.bridge.claude_code import ClaudeCodeEngine
from finresearch.bridge.limits import LimitTracker
from finresearch.bridge.ollama_engine import OllamaEngine
from finresearch.bridge.router import AllTiersFailed, BridgeRouter
from finresearch.bridge.types import (
    AgentResult,
    AgentTask,
    Capability,
    EngineUnavailable,
    ModelClass,
    Tier,
)
from finresearch.config import Settings, get_settings

__all__ = [
    "AgentResult",
    "AgentTask",
    "AllTiersFailed",
    "BridgeRouter",
    "Capability",
    "ModelClass",
    "Tier",
    "build_router",
]


def build_router(settings: Settings | None = None) -> BridgeRouter:
    """Wire engines from settings. The API tier is only added when enabled *and* a key exists."""
    s = settings or get_settings()
    engines = {
        Tier.CLAUDE_MAX: ClaudeCodeEngine(
            Tier.CLAUDE_MAX, claude_bin=s.claude_bin, models=s.claude_models, runs_dir=s.runs_dir
        ),
        Tier.LOCAL: OllamaEngine(
            base_url=s.ollama_url,
            models=s.local_models,
            low_memory_model=s.local_low_memory_model,
            min_free_gb_for_large=s.local_min_free_gb_for_9b,
            embed_model=s.local_embed_model,
            ocr_model=s.local_ocr_model,
            num_ctx=s.local_num_ctx,
            keep_alive=s.local_keep_alive,
            timeout_s=s.local_timeout_s,
            repair_attempts=s.local_schema_repair_attempts,
            max_prompt_chars=s.local_max_prompt_chars,
        ),
    }
    if s.claude_api_enabled and s.anthropic_api_key:
        with contextlib.suppress(EngineUnavailable):
            engines[Tier.CLAUDE_API] = ClaudeCodeEngine(
                Tier.CLAUDE_API,
                claude_bin=s.claude_bin,
                models=s.claude_models,
                api_key=s.anthropic_api_key.get_secret_value(),
                runs_dir=s.runs_dir,
            )
    tracker = LimitTracker(
        s.state_dir,
        five_hour_ceiling=s.max_five_hour_ceiling,
        seven_day_ceiling=s.max_seven_day_ceiling,
        breaker_failures=s.breaker_failures,
        breaker_cooldown_s=s.breaker_cooldown_s,
    )
    return BridgeRouter(engines, s.tier_order, tracker, s.state_dir / "ledger.jsonl")

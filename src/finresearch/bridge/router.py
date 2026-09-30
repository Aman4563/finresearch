"""The Claude Bridge router: tries tiers in order with pre-flight limit checks, failover and a call ledger.

Policy
------
1. CLAUDE_MAX first, unless the limit tracker says the subscription window is exhausted / near its ceiling,
   or the circuit breaker is open.
2. CLAUDE_API only if enabled AND a key is configured (currently: off).
3. LOCAL as last resort. Results are marked `degraded=True`; tasks needing web/tools/long context are refused
   (CapabilityMismatch) instead of producing low-quality fakes. Tasks with allow_degraded=False never fall
   to local.
Every attempt (success or failure) is appended to data/state/ledger.jsonl.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Protocol

from finresearch.bridge.limits import LimitTracker
from finresearch.bridge.types import (
    AgentResult,
    AgentTask,
    CapabilityMismatch,
    EngineError,
    EngineUnavailable,
    LimitReached,
    SchemaViolation,
    TaskFailed,
    Tier,
    TransientError,
)

# transient failures of this machine rather than of the service: they do not count towards the circuit breaker
LOCAL_TRANSIENT_KINDS = frozenset({"sleep", "oauth", "idle"})


class Engine(Protocol):
    tier: Tier

    async def run(self, task: AgentTask) -> AgentResult: ...

    async def health(self) -> dict: ...


class AllTiersFailed(Exception):
    def __init__(self, task: str, attempts: list[str], errors: list[EngineError] | None = None):
        super().__init__(f"{task}: no tier could complete the task -> " + "; ".join(attempts))
        self.attempts = attempts
        self.errors = errors or []  # the engine errors behind the attempts, for callers that retry or pause

    @property
    def limited(self) -> bool:
        """A tier hit its plan limit or was skipped for it (a cool-down, a pre-flight ceiling or an open circuit)."""
        return any(
            a.endswith(":limit") or ("skipped(" in a and "allow_degraded" not in a) for a in self.attempts
        )

    @property
    def transient(self) -> TransientError | None:
        """The transient error, when that alone is why the task failed: running it again later may work."""
        if self.limited:
            return None
        tr = [e for e in self.errors if isinstance(e, TransientError)]
        return tr[-1] if tr and len(tr) == len(self.errors) else None


class BridgeRouter:
    def __init__(self, engines: dict[Tier, Engine], order: list[Tier], tracker: LimitTracker, ledger: Path):
        self.engines = engines
        self.order = [t for t in order if t in engines]
        self.tracker = tracker
        self.ledger = Path(ledger)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    async def run(self, task: AgentTask, *, force_tier: Tier | None = None) -> AgentResult:
        tiers = [force_tier] if force_tier else self.order
        attempts: list[str] = []
        errors: list[EngineError] = []
        for tier in tiers:
            engine = self.engines.get(tier)
            if engine is None:
                attempts.append(f"{tier.value}:not-configured")
                continue
            if tier is Tier.LOCAL and not task.allow_degraded and not force_tier:
                attempts.append("local:skipped(allow_degraded=False)")
                continue
            ok, why = self.tracker.availability(tier)
            if not ok and not force_tier:
                attempts.append(f"{tier.value}:skipped({why})")
                continue

            started = time.time()
            try:
                result = await engine.run(task)
            except LimitReached as e:
                self.tracker.record_limit(tier, e.resets_at, str(e))
                attempts.append(f"{tier.value}:limit")
                self._log(task, tier, started, "limit", str(e))
                continue
            except TransientError as e:
                # a Mac that slept or two processes refreshing the login say nothing about the service's health
                if e.kind not in LOCAL_TRANSIENT_KINDS:
                    self.tracker.record_transient_failure(tier, str(e))
                attempts.append(f"{tier.value}:transient({e})")
                errors.append(e)
                self._log(task, tier, started, "transient", str(e))
                continue
            except (EngineUnavailable, CapabilityMismatch, SchemaViolation) as e:
                attempts.append(f"{tier.value}:{type(e).__name__}({e})")
                errors.append(e)
                self._log(task, tier, started, type(e).__name__, str(e))
                continue
            except TaskFailed as e:
                self._log(task, tier, started, "task_failed", str(e))
                raise
            except EngineError as e:  # any other engine error: try next tier
                attempts.append(f"{tier.value}:{type(e).__name__}({e})")
                errors.append(e)
                self._log(task, tier, started, type(e).__name__, str(e))
                continue

            self.tracker.record_success(tier)
            if result.rate_limit is not None:
                self.tracker.record_snapshot(tier, result.rate_limit)
            if tier is Tier.LOCAL:
                result.degraded = True
                prior = [a for a in attempts if not a.startswith("local")]
                result.degraded_reason = "local model fallback" + (
                    f" after: {'; '.join(prior)}" if prior else ""
                )
            result.attempts = [*attempts, f"{tier.value}:ok"]
            self._log(task, tier, started, "ok", None, result)
            return result
        raise AllTiersFailed(task.name, attempts, errors)

    async def health(self) -> dict[str, dict]:
        report: dict[str, dict] = {}
        for tier in self.order:
            try:
                h = await self.engines[tier].health()
                h["usable_now"], h["blocked_reason"] = self.tracker.availability(tier)
                report[tier.value] = h
            except EngineError as e:
                report[tier.value] = {"tier": tier.value, "error": str(e), "usable_now": False}
        return report

    def _log(self, task: AgentTask, tier: Tier, started: float, outcome: str, error: str | None,
             result: AgentResult | None = None) -> None:  # fmt: skip
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "task": task.name,
            "tier": tier.value,
            "outcome": outcome,
            "error": error[:500] if error else None,
            "seconds": round(time.time() - started, 2),
        }
        if result:
            rec |= {
                "model": result.model,
                "cost_usd_est": round(result.cost_usd_estimate, 4),
                "in_tokens": result.input_tokens,
                "out_tokens": result.output_tokens,
                "degraded": result.degraded,
                "five_hour_util": result.rate_limit.five_hour_utilization if result.rate_limit else None,
            }
        with open(self.ledger, "a") as f:
            f.write(json.dumps(rec) + "\n")

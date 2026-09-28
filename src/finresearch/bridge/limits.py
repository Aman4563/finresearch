"""Persistent per-tier health state: subscription-window utilisation, limit cool-downs and circuit breakers.

State lives in a small JSON file guarded by an advisory lock so several worker processes share one view.
"""

from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from finresearch.bridge.types import RateLimitSnapshot, Tier

DEFAULT_LIMIT_COOLDOWN_S = 3600  # used when a limit is hit but no reset time is reported


class LimitTracker:
    def __init__(
        self,
        state_dir: Path,
        *,
        five_hour_ceiling: float = 0.92,
        seven_day_ceiling: float = 0.97,
        breaker_failures: int = 3,
        breaker_cooldown_s: int = 600,
        clock=time.time,
    ):
        self.path = Path(state_dir) / "tier_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.five_hour_ceiling = five_hour_ceiling
        self.seven_day_ceiling = seven_day_ceiling
        self.breaker_failures = breaker_failures
        self.breaker_cooldown_s = breaker_cooldown_s
        self.clock = clock

    # ------------------------------------------------------------ storage
    @contextmanager
    def _locked(self):
        lock_path = self.path.with_suffix(".lock")
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.path.read_text()) if self.path.exists() else {}
            except json.JSONDecodeError:
                state = {}
            yield state
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
            tmp.replace(self.path)
            fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _tier(state: dict[str, Any], tier: Tier) -> dict[str, Any]:
        return state.setdefault(tier.value, {"cooling_until": 0, "failures": 0, "reason": None})

    # ------------------------------------------------------------ queries
    def availability(self, tier: Tier) -> tuple[bool, str | None]:
        """Return (usable_now, reason_if_not)."""
        now = self.clock()
        with self._locked() as state:
            t = self._tier(state, tier)
            if t.get("cooling_until", 0) > now:
                until = time.strftime("%H:%M", time.localtime(t["cooling_until"]))
                return False, f"{t.get('reason') or 'cooling down'} (until {until})"
            snap = t.get("snapshot")
            if tier is Tier.CLAUDE_MAX and snap:
                s = RateLimitSnapshot(**snap)
                if s.status == "rejected" and (s.resets_at or 0) > now:
                    return False, "subscription limit reported as rejected"
                if (
                    s.five_hour_utilization is not None
                    and s.five_hour_utilization >= self.five_hour_ceiling
                    and (s.five_hour_resets_at or 0) > now
                ):
                    return False, f"5-hour window at {s.five_hour_utilization:.0%} (pre-flight ceiling)"
                if (
                    s.seven_day_utilization is not None
                    and s.seven_day_utilization >= self.seven_day_ceiling
                    and (s.seven_day_resets_at or 0) > now
                ):
                    return False, f"7-day window at {s.seven_day_utilization:.0%} (pre-flight ceiling)"
            return True, None

    def snapshot(self, tier: Tier) -> RateLimitSnapshot | None:
        with self._locked() as state:
            snap = self._tier(state, tier).get("snapshot")
            return RateLimitSnapshot(**snap) if snap else None

    def describe(self) -> dict[str, Any]:
        with self._locked() as state:
            return json.loads(json.dumps(state))

    # ------------------------------------------------------------ updates
    def record_snapshot(self, tier: Tier, snap: RateLimitSnapshot) -> None:
        with self._locked() as state:
            self._tier(state, tier)["snapshot"] = snap.model_dump()

    def record_success(self, tier: Tier) -> None:
        with self._locked() as state:
            t = self._tier(state, tier)
            t["failures"] = 0
            if t.get("cooling_until", 0) <= self.clock():
                t["reason"] = None

    def record_limit(self, tier: Tier, resets_at: int | None, reason: str) -> None:
        now = self.clock()
        until = resets_at if resets_at and resets_at > now else now + DEFAULT_LIMIT_COOLDOWN_S
        with self._locked() as state:
            t = self._tier(state, tier)
            t["cooling_until"] = until
            t["reason"] = reason

    def record_transient_failure(self, tier: Tier, reason: str) -> None:
        with self._locked() as state:
            t = self._tier(state, tier)
            t["failures"] = t.get("failures", 0) + 1
            if t["failures"] >= self.breaker_failures:
                t["cooling_until"] = self.clock() + self.breaker_cooldown_s
                t["reason"] = f"circuit open after {t['failures']} failures: {reason}"
                t["failures"] = 0

    def reset(self, tier: Tier | None = None) -> None:
        with self._locked() as state:
            for key in [tier.value] if tier else list(state):
                state.pop(key, None)

"""The model inventory (#379): every model or rule with its tier, computed status, outcome metric, limits of use and
the regime changes that make evidence stale. Read-only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI


def add_model_inventory_routes(app: FastAPI, *, clock=None) -> None:
    now = clock or (lambda: datetime.now(UTC))

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        from finresearch.fincalc.dates import to_ist
        from finresearch.inventory import regimes, registry

        today = to_ist(now()).date()
        return {"as_of": today.isoformat(), "limits_of_use": registry.LIMITS_OF_USE, "tiers": registry.TIER_DEPTH,
                "max_evidence_age_days": regimes.MAX_EVIDENCE_AGE_DAYS,
                "regime_changes": [{"day": c.day.isoformat(), "tag": c.tag, "what": c.what, "source": c.source}
                                   for c in regimes.regime_changes()],
                "entries": [e.to_json(today) for e in registry.entries()]}  # fmt: skip

"""The model inventory (#379): every model or rule with its tier, computed status, outcome metric, limits of use and
the regime changes that make evidence stale; each entry's evidence date and age (unknown stays None, never 0), its
filed red-team reviews and its latest monthly re-validation by regime split. Read-only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI


def add_model_inventory_routes(app: FastAPI, *, clock=None) -> None:
    now = clock or (lambda: datetime.now(UTC))

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        from finresearch.backtest import store
        from finresearch.db import session_scope
        from finresearch.fincalc.dates import to_ist
        from finresearch.inventory import redteam, regimes, registry
        from finresearch.inventory import revalidate as rv
        from finresearch.monitor import revalidate as rvj

        today = to_ist(now()).date()
        reviews = redteam.scan()
        with session_scope() as s:
            run = store.latest_run(s)
            run_day = regimes.run_day(run.created_at) if run is not None else None
            run_tags = (run.detail or {}).get("regime_tags") if run is not None else None
            latest = rv.latest_by_model(s)
        stale = regimes.staleness(run_day, today, tags=run_tags) if run is not None else None
        out = []
        for e in registry.entries():
            filed = reviews.by_model.get(e.key, [])
            row = e.to_json(today)
            row["status"], row["status_reason"] = e.status(today, reviews=len(filed) + len(e.red_team))
            row["red_team"] = [*({"path": x} for x in e.red_team), *(r.to_json() for r in filed)]
            as_of = run_day if e.kind == "recipe" else e.validated_on
            row["evidence_as_of"] = as_of.isoformat() if as_of else None
            row["evidence_age_days"] = (today - as_of).days if as_of else None
            row["evidence_stale"] = stale if e.kind == "recipe" else None
            row["revalidation"] = latest.get(e.key)
            out.append(row)
        return {"as_of": today.isoformat(), "limits_of_use": registry.LIMITS_OF_USE, "tiers": registry.TIER_DEPTH,
                "max_evidence_age_days": regimes.MAX_EVIDENCE_AGE_DAYS,
                "regime_changes": [{"day": c.day.isoformat(), "tag": c.tag, "what": c.what, "source": c.source}
                                   for c in regimes.regime_changes()],
                "revalidation": {"after": f"{rvj.RUN_AFTER:%H:%M}", "every": "month",
                                 "splits": [sp.key for sp in rv.splits()]},
                "redteam_invalid": [{"path": p, "why": w} for p, w in reviews.invalid],
                "entries": out}  # fmt: skip

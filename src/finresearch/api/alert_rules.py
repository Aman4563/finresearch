"""Alert rules for every asset and notification channels (roadmap item 10).

Rules themselves are part of the profile (`alert_rules`, saved with PUT /api/profile). These routes serve the metric
registry, each rule's current state, a "check now", the channel settings (secrets masked), a test send and the
delivery log."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from finresearch.db import session_scope


class TestBody(BaseModel):
    channel: str


def add_alert_rule_routes(app: FastAPI, clock: Callable[[], datetime] | None = None) -> None:
    def now() -> datetime:
        return clock() if clock else datetime.now(UTC)

    @app.get("/api/alert-rules/registry")
    def alert_registry() -> dict[str, Any]:
        """Every alert metric by asset kind (label, unit, cadence, description, source) and the templates."""
        from finresearch.alerts.registry import registry_json

        return registry_json()

    @app.get("/api/alert-rules/state")
    def alert_rule_state() -> list[dict[str, Any]]:
        """Where each rule stands per instrument: fired / clear / unknown, last value, last fired."""
        from finresearch.alerts.engine import state_json
        from finresearch.db.models import AlertRuleState

        with session_scope() as s:
            rows = s.scalars(
                select(AlertRuleState).order_by(AlertRuleState.rule_id, AlertRuleState.instrument)
            )
            return [state_json(r) for r in rows]

    @app.post("/api/alert-rules/{rule_id}/check")
    async def check_rule(rule_id: str) -> dict[str, Any]:
        """Evaluate one saved rule now, whatever its cadence (fires and notifies like a scheduled check)."""
        from finresearch.alerts.engine import evaluate
        from finresearch.suggest.advisor import load_profile

        with session_scope() as s:
            rule = next((r for r in load_profile(s).alert_rules if r.id == rule_id), None)
            if rule is None:
                raise HTTPException(404, f"no saved alert rule {rule_id!r}")
            res = await evaluate(s, [rule.model_copy(update={"enabled": True})], now())
            return res

    @app.get("/api/notifications")
    def notifications() -> dict[str, Any]:
        """Channel settings with secrets masked (a secret is never returned)."""
        from finresearch.monitor import notify

        with session_scope() as s:
            return notify.public(s)

    @app.put("/api/notifications")
    def put_notifications(body: dict[str, Any]) -> dict[str, Any]:
        """Change channel settings. A masked secret sent back keeps the stored one."""
        from finresearch.monitor import notify

        try:
            with session_scope() as s:
                return notify.update(s, body)
        except ValidationError as e:
            raise HTTPException(
                422, e.errors(include_url=False, include_context=False, include_input=False)
            ) from e

    @app.get("/api/notifications/random-topic")
    def random_topic() -> dict[str, str]:
        from finresearch.monitor.notify import random_topic

        return {"topic": random_topic()}

    @app.post("/api/notifications/test")
    async def test_notification(body: TestBody) -> dict[str, Any]:
        """Send a test notification to one channel now and return the logged delivery (with its error, if any)."""
        from finresearch.monitor.notify import send_test

        try:
            return await send_test(body.channel, now())
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/notifications/deliveries")
    def deliveries(limit: int = Query(50, ge=1, le=500), status: str | None = None) -> list[dict[str, Any]]:
        """The delivery log, newest first, with rule and alert context."""
        from finresearch.db.models import Alert, AlertDelivery
        from finresearch.monitor.notify import delivery_json

        with session_scope() as s:
            q = select(AlertDelivery, Alert).join(Alert, Alert.id == AlertDelivery.alert_id, isouter=True)
            if status:
                q = q.where(AlertDelivery.status == status)
            return [{**delivery_json(d), "rule_id": (a.data or {}).get("rule_id") if a else None,
                     "alert_kind": a.kind if a else None}
                    for d, a in s.execute(q.order_by(AlertDelivery.id.desc()).limit(limit))]  # fmt: skip

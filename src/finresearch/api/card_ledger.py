"""The card ledger's report and the override log (#376): GET /api/fno/card-ledger (the scoring report, read only),
GET /api/fno/overrides (every recorded decision) and POST /api/fno/overrides (append one decision; the only write).

The override log is personal (fnobook.overrides); nothing here sends it anywhere."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from finresearch.db import session_scope


class OverrideIn(BaseModel):
    card_log_id: int = Field(ge=1)
    card_index: int = Field(ge=0, le=100)
    decision: Literal["traded", "skipped"]
    note: str | None = Field(None, max_length=500)


def add_card_ledger_routes(app: FastAPI) -> None:
    @app.get("/api/fno/card-ledger")
    def card_ledger() -> dict[str, Any]:
        """The registered card ledger report (evals/experiments/card_ledger), the override log and the panel."""
        from finresearch.fnobook import card_report

        with session_scope() as s:
            return card_report.report(s)

    @app.get("/api/fno/overrides")
    def overrides_list() -> dict[str, Any]:
        from finresearch.fnobook import overrides as ov

        with session_scope() as s:
            rows = ov.history(s)
        return {"overrides": rows[::-1], "total": len(rows)}

    @app.post("/api/fno/overrides")
    def overrides_add(body: OverrideIn) -> dict[str, Any]:
        """Record that the owner traded or skipped a logged card (an unknown card is a 422)."""
        from finresearch.fnobook import overrides as ov

        with session_scope() as s:
            return ov.record(s, card_log_id=body.card_log_id, card_index=body.card_index, decision=body.decision,
                             note=body.note)  # fmt: skip

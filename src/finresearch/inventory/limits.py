"""The Kupiec trigger's mark in the model inventory (#378, skill-monitor-v2): which risk models are "not for limits".

Computed on read from the forecast ledger's logged VaR rows (skillmon.risk), never stored or declared: a registered
model (skillmon/specs_v2.py RISK_MODELS) reads "not for limits" once its VaR failed Kupiec's test (p < 0.05) in 3
consecutive quarters, and the mark stays; until then it reads "insufficient data", "unknown" or "not triggered", never
fine by default. Every other entry has no mark (None). A changed v2 registration is refused, so every mark then reads
unknown. Read only: GET /api/models and `finresearch models list` call it."""

from __future__ import annotations

from datetime import date
from typing import Any

from finresearch.skillmon import specs_v2

RULE = (f"Kupiec p < {specs_v2.RISK.alpha} in {specs_v2.RISK.consecutive} consecutive quarters marks the model 'not for "
        "limits' (skill-monitor-v2)")  # fmt: skip


def marks(s: Any, today: date) -> dict[str, dict[str, Any]]:
    """Inventory key -> its trigger (skillmon.risk.marks), for the registered risk models only."""
    from finresearch.skillmon import monitor, risk

    try:
        monitor.require_registered_v2()
    except monitor.PreregistrationError as e:
        return {k: {"status": "unknown", "why": str(e), "run": None, "tested": 0, "quarters": []}
                for k in specs_v2.RISK_MODELS}  # fmt: skip
    return risk.marks(s, today)


def to_json(m: dict[str, Any] | None) -> dict[str, Any] | None:
    """The API's compact form of one mark (None for an entry without one)."""
    if m is None:
        return None
    qs = [
        {k: q[k] for k in ("quarter", "days", "exceptions", "p", "status")} for q in m.get("quarters") or []
    ]
    return {"status": m["status"], "why": m["why"], "run": m.get("run"), "tested": m.get("tested", 0), "rule": RULE,
            "version": specs_v2.SPEC_VERSION, "quarters": qs}  # fmt: skip

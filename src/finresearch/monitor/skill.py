"""The monitor's nightly skill pass (#378, finresearch.skillmon): database only, no network.

Runs once a day from RUN_AFTER (21:30 IST), after the evening forecast resolutions (signals.ledger READY_AFTER_IST,
16:30, and the card ledger's bhavcopy replay after the 20:30 bhavcopy). Every calendar day, holidays included: it
reads only the forecast ledger, so an unknown holiday list cannot make it wrong, and skipping would only make the
brief's report staler. Claimed in `alert_eval_slot` ("skill:<day>"), whose result keeps the report for the brief; a
crash is retried (monitor.disclosures MAX_ATTEMPTS, RETRY_AFTER apart). Alarms are results, not a failed pass.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(21, 30)
STALE_AFTER = timedelta(hours=36)  # a missed night (21:30 -> the second 08:30 brief) is stale
PREFIX = "skill:"


def due_slot(now: datetime) -> str | None:
    ist = to_ist(now)
    return f"{PREFIX}{ist.date().isoformat()}" if ist.time() >= RUN_AFTER else None


def skill_step(now: datetime) -> dict[str, Any]:
    from finresearch.db import session_scope
    from finresearch.monitor.disclosures import claim, finish
    from finresearch.skillmon import monitor

    slot = due_slot(now)
    if slot is None or not claim(slot, now):
        return {}
    try:
        with session_scope() as s:
            rep = monitor.report(s, now)
    except Exception as e:
        log.warning("skill monitor pass failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"skill": "failed"}
    finish(slot, rep, now, failed=False)
    return {"skill": {"counts": rep["counts"], "alarms": rep["alarms"]}}


def latest(s: Session) -> dict[str, Any] | None:
    """The newest finished pass's report (read only), or None when none has finished."""
    from finresearch.db.models import AlertEvalSlot

    rows = s.scalars(select(AlertEvalSlot).where(AlertEvalSlot.slot.like(f"{PREFIX}%"))
                     .order_by(AlertEvalSlot.slot.desc()).limit(10))  # fmt: skip
    return next((dict(r.result) for r in rows if (r.result or {}).get("status") == "done"), None)


def brief_section(s: Session, now: datetime) -> dict[str, Any]:
    """The brief's skill section: alarms (red), cells that could not be checked, the cells with too few outcomes,
    and whether the stored report is missing or stale. Never "ok" by default."""
    rep = latest(s)
    if rep is None:
        return {"status": "not run", "alarms": [], "lines": [{"level": "info", "text": "The forecast skill monitor "
                "has not run yet: calibration drift and skill against the benchmarks are unchecked."}]}  # fmt: skip
    at = datetime.fromisoformat(rep["generated_at"])
    lines: list[dict[str, str]] = []
    status = "current"
    if now - at > STALE_AFTER:
        status = "stale"
        lines.append({"level": "info", "text": f"The forecast skill monitor last ran on {to_ist(at).date()}: alarms "
                      "since then are unknown."})  # fmt: skip
    cells = rep.get("cells") or []
    alarms = [{"label": c["label"], **a} for c in cells for a in c.get("alarms") or []]
    lines += [{"level": "warn", "text": f"Forecast skill alarm, {a['label']}: {a['text']}."} for a in alarms]
    lines += [{"level": "info", "text": f"{c['label']}: a skill check could not be checked (unknown, not ok)."}
              for c in cells if c["status"] == "unknown"]  # fmt: skip
    few = [c for c in cells if c["status"] == "insufficient data"]
    if few:
        shown = "; ".join(f"{c['label']} {c['independent']} of {c['trigger']}" for c in few[:3])
        more = f" and {len(few) - 3} more" if len(few) > 3 else ""
        lines.append({"level": "info", "text": f"{len(few)} forecast cell(s) have insufficient data to monitor "
                      f"(resolved independent events: {shown}{more}): not ok, just unchecked."})  # fmt: skip
    if not cells:
        lines.append(
            {"level": "info", "text": "No logged forecast with a probability yet: nothing to monitor."}
        )
    return {"status": status, "generated_at": rep["generated_at"], "version": rep.get("version"),
            "counts": rep.get("counts"), "alarms": alarms, "lines": lines}  # fmt: skip

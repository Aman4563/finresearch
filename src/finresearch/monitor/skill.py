"""The monitor's nightly skill pass (#378, finresearch.skillmon): database only, no network.

Runs once a day from RUN_AFTER (21:30 IST), after the evening forecast resolutions (signals.ledger READY_AFTER_IST,
16:30, and the card ledger's bhavcopy replay after the 20:30 bhavcopy). Every calendar day, holidays included: it
reads only the forecast ledger, so an unknown holiday list cannot make it wrong, and skipping would only make the
brief's report staler. Claimed in `alert_eval_slot` ("skill:<day>"), whose result keeps the report for the brief; a
crash is retried (monitor.disclosures MAX_ATTEMPTS, RETRY_AFTER apart). Alarms are results, not a failed pass.

skill-monitor-v2 (skillmon/specs_v2.py) adds the report's "v2" key: input drift of the direction model and the card
(read only from the stored F&O history) and the VaR models' Kupiec mark. It runs after the v1 cells and fails soft: a
refused registration or an error reads unchecked in the brief and never costs the cell checks.
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
            rep["v2"] = _v2(s, now)
    except Exception as e:
        log.warning("skill monitor pass failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"skill": "failed"}
    finish(slot, rep, now, failed=False)
    return {
        "skill": {"counts": rep["counts"], "alarms": rep["alarms"], "v2_alarms": rep["v2"].get("alarms", 0)}
    }


def _v2(s: Session, now: datetime) -> dict[str, Any]:
    """skill-monitor-v2's sections (skillmon.monitor.report_v2); refused or failed, it says so and v1 still stands."""
    from finresearch.skillmon import monitor

    try:
        return monitor.report_v2(s, now)
    except monitor.PreregistrationError as e:
        return {"status": "refused", "error": str(e)[:300], "alarms": 0}
    except Exception as e:
        log.warning("skill-monitor-v2 sections failed", exc_info=True)
        return {"status": "unknown", "error": f"{type(e).__name__}: {e}"[:300], "alarms": 0}


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
    v2_alarms, v2_lines = v2_brief(rep.get("v2"))
    alarms += v2_alarms
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
            {
                "level": "info",
                "text": "No logged forecast with a probability yet: no forecast cell to monitor.",
            }
        )
    lines += v2_lines
    return {"status": status, "generated_at": rep["generated_at"], "version": rep.get("version"),
            "counts": rep.get("counts"), "alarms": alarms, "lines": lines}  # fmt: skip


def v2_brief(v2: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """(alarms, info lines) of skill-monitor-v2's sections: an input at act or a VaR model not for limits is an
    alarm; inputs and models that could not be checked are said so, never ok (specs_v2.BRIEF)."""
    from finresearch.skillmon.specs_v2 import DRIFT

    def info(text: str) -> dict[str, str]:
        return {"level": "info", "text": text}

    if v2 is None:
        return [], [info("Input drift and the VaR models' Kupiec trigger have not run yet (this report is from before "
                         "skill-monitor-v2): unchecked, not ok.")]  # fmt: skip
    if v2.get("status") in ("refused", "unknown"):
        return [], [info(f"skill-monitor-v2 could not run ({v2.get('error')}): input drift and the VaR models' "
                         "Kupiec trigger are unchecked.")]  # fmt: skip
    alarms: list[dict[str, Any]] = []
    lines: list[dict[str, str]] = []
    d = v2.get("drift") or {}
    if d.get("status") == "unknown":
        lines.append(info(f"Input drift could not be checked ({d.get('error')}): unknown, not ok."))
    groups = d.get("groups") or []
    for g in (g for g in groups if g["status"] == "act"):
        shown = ", ".join(f"{a['input']} {a['psi']:.2f}" for a in g["act"][:4])
        more = f" and {len(g['act']) - 4} more" if len(g["act"]) > 4 else ""
        ref = g.get("reference") or ["?", "?"]
        alarms.append({"label": g["label"], "check": "drift", "text": f"input drift, PSI >= {DRIFT.act:.2f} on "
                       f"{shown}{more} (live sessions to {g.get('live_to')} against {ref[0]} to {ref[1]})"})  # fmt: skip
    watch = [(g, w) for g in groups for w in g.get("watch") or []]
    if watch:
        shown = "; ".join(f"{g['label']} {w['input']} {w['psi']:.2f}" for g, w in watch[:3])
        more = f" and {len(watch) - 3} more" if len(watch) > 3 else ""
        lines.append(
            info(f"{len(watch)} input(s) on watch (PSI {DRIFT.watch:.2f} to {DRIFT.act:.2f}): {shown}{more}.")
        )
    few = [(g, n) for g in groups for n in g.get("insufficient") or []]
    if few:
        shown = "; ".join(f"{g['label']} {n}" for g, n in few[:3]) + (" and more" if len(few) > 3 else "")
        lines.append(info(f"{len(few)} input(s) have too few known values for a drift check ({shown}): not ok, just "
                          "unchecked."))  # fmt: skip
    lines += [
        info(f"Input drift of {g['label']} is unknown: {g.get('why')}.")
        for g in groups
        if g["status"] == "unknown"
    ]
    alarms_r, lines_r = _risk_brief(v2.get("risk") or {})
    return alarms + alarms_r, lines + lines_r


def _risk_brief(r: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    from finresearch.inventory.registry import entry

    def name(key: str) -> str:
        e = entry(key)
        return e.name if e else key

    lines = []
    if r.get("status") == "unknown":
        lines.append({"level": "info", "text": f"The VaR models' Kupiec trigger could not be checked "
                      f"({r.get('error')}): unknown, not ok."})  # fmt: skip
    marks = r.get("marks") or {}
    alarms = [{"label": name(k), "check": "kupiec", "text": f"not for limits: {m['why']}"}
              for k, m in marks.items() if m["status"] == "not for limits"]  # fmt: skip
    unchecked: dict[tuple[str, str], list[str]] = {}
    for k, m in marks.items():
        if m["status"] in ("insufficient data", "unknown"):
            unchecked.setdefault((m["status"], m["why"]), []).append(name(k))
    lines += [{"level": "info", "text": f"{' and '.join(names)}: the Kupiec trigger reads {st} ({why}): not ok, just "
               "unchecked."} for (st, why), names in unchecked.items()]  # fmt: skip
    return alarms, lines

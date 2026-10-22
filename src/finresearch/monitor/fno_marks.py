"""The nightly F&O marks (#383, fnobook.marks): after tonight's bhavcopy is loaded (monitor.fno_bhav), one mark per
open paper leg and per Groww F&O position leg. Claimed in `alert_eval_slot` ("fno_marks:<day>") so two monitor
processes never mark twice. No network beyond Groww's instruments file (cached) to resolve real contracts.

Real positions are read from the last Groww sync or in-session poll (`fnobook.service.stored_positions`); Groww keeps
only their latest state, so they are marked only when that read was made today (IST): an older read may hold
positions closed since, and those are not marked rather than guessed."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)


async def real_legs(today: date, market: Any, stored: dict[str, Any]) -> tuple[list[Any], int, str | None]:
    """(resolved legs, rows not in the instruments file, why none were read)."""
    from finresearch.fnobook import marks, service
    from finresearch.fnobook import positions as pos

    if stored.get("status") != "ok":
        return [], 0, f"Groww positions {stored.get('status', 'not read')}"
    at = stored.get("at")
    if not at or to_ist(datetime.fromisoformat(at)).date() != today:
        return [], 0, f"Groww positions not read today (last read {at or 'never'})"
    if not stored.get("rows"):
        return [], 0, None
    inst = await service._instruments(market)
    if inst is None:
        return [], len(stored["rows"]), "Groww's instruments file is unavailable"
    out, missing = [], 0
    for r in stored["rows"]:
        c = pos.contract(inst.contract(r.get("symbol") or ""))
        if c is None:
            missing += 1
            continue
        q = service._f(r.get("quantity"))
        qty = int(q) if r.get("direction_known") and q is not None else None
        out.append(marks.RealLeg(r["symbol"], c["underlying"], c["expiry"], c["right"], c["strike"], qty))
    return out, missing, None


async def fno_marks_step(now: datetime, holidays: set[date] | None = None, *, market: Any = None,
                         stored: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    from finresearch.db import session_scope
    from finresearch.fnobook import marks, service
    from finresearch.monitor.disclosures import claim, finish
    from finresearch.monitor.fno_bhav import _loaded, due_slot

    bhav = due_slot(now, holidays)
    today = to_ist(now).date()
    if bhav is None or not _loaded(today):
        return {}
    slot = bhav.replace("fno_bhav:", "fno_marks:")
    if not claim(slot, now):
        return {}
    try:
        st = stored if stored is not None else service.stored_positions()
        legs, missing, why = await real_legs(today, market or service._market(), st)
        with session_scope() as s:
            out: dict[str, Any] = marks.record(s, today, legs)
        out |= {"real_unresolved": missing, "real_note": why}
    except Exception as e:
        log.warning("F&O marks failed", exc_info=True)
        finish(slot, {"error": type(e).__name__}, now, failed=True)
        return {"fno_marks": {"error": type(e).__name__}}
    finish(slot, out, now, failed=False)
    return {"fno_marks": out}

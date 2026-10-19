"""The nightly F&O direction forecasts (#327, direction.live): after tonight's bhavcopy is loaded (monitor.fno_bhav), log
one forecast per underlying × horizon × event for today's close to the forecast ledger, which resolves them h sessions
later. Claimed in `alert_eval_slot` ("fno_direction:<day>") so two monitor processes never log twice; runs only when
today's bhavcopy is loaded (the features need today's close)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)


async def fno_direction_step(now: datetime, holidays: set[date] | None = None) -> dict[str, Any]:
    from finresearch.direction import live, specs
    from finresearch.monitor.disclosures import claim, finish
    from finresearch.monitor.fno_bhav import _loaded, due_slot

    bhav = due_slot(now, holidays)
    today = to_ist(now).date()
    if bhav is None or not _loaded(today):
        return {}
    slot = bhav.replace("fno_bhav:", "fno_direction:")
    if not claim(slot, now):
        return {}
    out: dict[str, Any] = {}
    failed = False
    for sym in specs.UNDERLYINGS:
        try:
            st = live.state(sym, fresh=True)
            if st["as_of"] != today.isoformat():
                out[sym] = f"latest stored session is {st['as_of']}, not today"
                failed = True
                continue
            res = live.log(sym, now, st=st, holidays=holidays)
            out[sym] = {"logged": len(res["logged"]), "skipped": res["skipped"]}
        except Exception as e:  # one underlying failing never stops the other
            log.warning("F&O direction forecasts for %s failed", sym, exc_info=True)
            out[sym] = f"{type(e).__name__}: {e}"[:300]
            failed = True
    finish(slot, out, now, failed=failed)
    return {"fno_direction": out}

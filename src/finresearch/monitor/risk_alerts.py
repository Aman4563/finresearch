"""The monitor's nightly risk-alert pass (#398, alerts.risk): database only, no network.

Due on trading days from RUN_AFTER (20:45 IST), after tonight's F&O bhavcopy (monitor.fno_bhav, from 19:30) and the
book's marks (monitor.fno_marks) are written; it waits for both, but from DEADLINE (23:30 IST) it runs anyway, so a
file that never came raises "can't check" alerts instead of silence. Holiday policy, on purpose: an unknown NSE
holiday list counts as no holidays (like the alert rules): on a holiday the inputs then read unknown and say so,
whereas failing closed (monitor.market_days) would skip the pass without a word. Claimed in `alert_eval_slot`
("risk_alerts:<day>"); a crash is retried (monitor.disclosures MAX_ATTEMPTS, RETRY_AFTER apart). Alerts only:
nothing here places, modifies or cancels an order.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, time
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(20, 45)
DEADLINE = time(23, 30)
PREFIX = "risk_alerts:"


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    from finresearch.fincalc.dates import is_business_day

    ist = to_ist(now)
    if ist.time() < RUN_AFTER:
        return None
    if holidays is None:
        from finresearch.adapters.nse_holidays import trading_holidays

        holidays = (
            trading_holidays()
        )  # an unknown year's list is empty: no holidays (see the module docstring)
    return f"{PREFIX}{ist.date().isoformat()}" if is_business_day(ist.date(), holidays) else None


def ready(day: date) -> bool:
    """Tonight's bhavcopy is loaded and the book's marks are written."""
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot
    from finresearch.monitor.fno_bhav import _loaded

    if not _loaded(day):
        return False
    with session_scope() as s:
        row = s.get(AlertEvalSlot, f"fno_marks:{day.isoformat()}")
        return row is not None and (row.result or {}).get("status") == "done"


def evaluate(now: datetime, day: date, holidays: set[date] | None = None) -> dict[str, Any]:
    """Read every risk input for `day` and apply each rule (alerts.risk). Returns counts and the checks."""
    from sqlalchemy import select

    from finresearch.alerts import risk
    from finresearch.alerts import risk_readers as rr
    from finresearch.db import session_scope
    from finresearch.db.models import FnoMark
    from finresearch.fnobook import scenarios, service
    from finresearch.monitor.iv import INDEX_SYMBOLS
    from finresearch.suggest.advisor import load_profile

    try:
        funds, fnote = service.stored_funds(now)
    except Exception as e:  # unknown, never 0
        funds, fnote = None, f"the real book's funds could not be read ({type(e).__name__})"
    counts: Counter[str] = Counter()
    checks: list[dict[str, Any]] = []
    with session_scope() as s:
        try:
            shock, notes = scenarios.stored_shock(s, day, holidays=holidays)
        except Exception as e:
            shock, notes = None, [f"the open book could not be repriced ({type(e).__name__})"]
        try:
            es = scenarios.stored_es(s, day, holidays=holidays)
        except Exception as e:  # unknown, never 0
            es = (None, f"the book's expected shortfall could not be computed ({type(e).__name__})")
        capital = float(load_profile(s).fno_capital_inr)
        held = set(s.scalars(select(FnoMark.symbol).where(FnoMark.day == day)))
        rows = (rr.book_readings(shock, notes, capital, funds, fnote, book_open=bool(held), es=es)
                + rr.series_readings(s, sorted(set(INDEX_SYMBOLS) | held), day) + rr.leg_readings(s, day, now))  # fmt: skip
        for rid, inst, rd in rows:
            what = risk.apply(s, risk.RULES[rid], inst, rd, now)
            counts[what] += 1
            checks.append({"rule": rid, "instrument": inst, "result": what, "value": rd.value, "series": rd.series,
                           "detail": rd.detail[:300]})  # fmt: skip
    return {"counts": dict(counts), "checks": checks[:200]}


def risk_step(now: datetime, holidays: set[date] | None = None) -> dict[str, Any]:
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now, holidays)
    if slot is None:
        return {}
    day = to_ist(now).date()
    if to_ist(now).time() < DEADLINE and not ready(day):
        return {}
    if not claim(slot, now):
        return {}
    try:
        res = evaluate(now, day, holidays)
    except Exception as e:
        log.warning("risk-alert pass failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"risk_alerts": "failed"}
    finish(slot, res, now, failed=False)
    return {"risk_alerts": res["counts"]}


STALE_AFTER_DAYS = 4  # a long weekend plus a holiday; an older last pass is stale
MAX_LINES = 6


def brief_section(s: Any, now: datetime) -> dict[str, Any]:
    """The brief's risk section: alerts still standing (fired and not cleared), inputs that cannot be checked, the
    mute suggestions (alerts.quality) and whether the last pass is missing or stale. Never "ok" by default. Reads
    only. The new firings themselves are in the brief's alert list."""
    from sqlalchemy import select

    from finresearch.alerts import quality, risk
    from finresearch.db.models import AlertEvalSlot, AlertRuleState

    lines: list[dict[str, str]] = []
    rows = s.scalars(select(AlertEvalSlot).where(AlertEvalSlot.slot.like(f"{PREFIX}%"))
                     .order_by(AlertEvalSlot.slot.desc()).limit(10)).all()  # fmt: skip
    last = next((r.slot.removeprefix(PREFIX) for r in rows if (r.result or {}).get("status") == "done"), None)
    status = "current"
    if last is None:
        status = "not run"
        lines.append({"level": "info", "text": "The risk alerts have not run yet: limits, IV shocks, gaps and short "
                      "strikes are unchecked (not ok)."})  # fmt: skip
    elif (to_ist(now).date() - date.fromisoformat(last)).days > STALE_AFTER_DAYS:
        status = "stale"
        lines.append(
            {"level": "info", "text": f"The risk alerts last ran for {last}: anything since is unchecked."}
        )
    states = s.scalars(select(AlertRuleState).where(AlertRuleState.rule_id.like(f"{risk.PREFIX}%"),
                                                    AlertRuleState.status.in_(("fired", "unknown")))
                       .order_by(AlertRuleState.rule_id, AlertRuleState.instrument)).all()  # fmt: skip
    fired = [x for x in states if x.status == "fired"]
    unknown = [x for x in states if x.status == "unknown"]

    def label(x: AlertRuleState) -> str:
        r = risk.RULES.get(x.rule_id.removeprefix(risk.PREFIX))
        return f"{r.label if r else x.rule_id} ({'the F&O book' if x.instrument == 'BOOK' else x.instrument})"

    lines += [{"level": "warn", "text": f"Risk alert standing: {label(x)}, last reading {float(x.value):.2f}."}
              for x in fired[:MAX_LINES] if x.value is not None]  # fmt: skip
    lines += [{"level": "info", "text": f"Risk alert can't check {label(x)}: {x.note or 'the input is unknown'}."}
              for x in unknown[:MAX_LINES]]  # fmt: skip
    if len(unknown) > MAX_LINES:
        lines.append(
            {"level": "info", "text": f"{len(unknown) - MAX_LINES} more risk inputs cannot be checked."}
        )
    for q in quality.by_rule(s):
        if q["suggest_mute"]:
            lines.append({"level": "info", "text": f"Alert rule {q['label']}: you acted on {q['acted']} of "
                          f"{q['fired']} firings ({q['precision']:.0%}); consider muting it."})  # fmt: skip
    return {
        "status": status,
        "last_pass": last,
        "standing": len(fired),
        "unknown": len(unknown),
        "lines": lines,
    }

"""P4.5 (#398): risk alerts that matter, with severity tiers. Alerts only: nothing here places, modifies or cancels an
order. Local only: the F&O book is personal data and never reaches a model.

Each rule reads one statistic per instrument (fincalc.risk_alerts; the readers are in alerts.risk_readers) and keeps
its state in `alert_rule_state` under the rule id "risk:<rule>":
- a limit rule (mode "hysteresis") fires at U >= `fire` and clears only below `clear`; the others fire at >= `fire`;
- fired -> still true: nothing more (de-duplication); clear -> true again: fires again;
- an unknown input (None with the reason) raises one "can't check" alert when the state becomes unknown, never
  silence, and again only after a known reading has come in between.
Routing by tier: `urgent` firings are pushed at once to every configured channel with priority urgent (quiet hours do
not hold them); an unknown urgent-tier input is pushed at priority high (quiet hours hold it); `warn` and `info` are
only recorded, for the 08:30 brief (which lists every alert since the last one). The monitor's forwarder skips this
kind (monitor.notify.forward_monitor_alerts), so nothing is pushed twice. Each alert names the series it read.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import Alert, AlertRuleState
from finresearch.fincalc import risk_alerts as ra

RISK_KIND = "risk_alert"
PREFIX = "risk:"
LEVEL = {"urgent": "action", "warn": "warn", "info": "info"}


@dataclass(frozen=True)
class RiskRule:
    id: str
    category: (
        str  # limit | margin | es | gap_stop | shock | gap | touch | event (ra.NEVER_MUTE are never muted)
    )
    tier: str  # urgent | warn | info
    label: str
    fire: float
    clear: float | None = None  # set = hysteresis
    unit: str = ""  # "x" (a ratio or z), "%" (a probability shown as %), "" (a 0/1 flag)
    path: str = "/fno"


_R = RiskRule
RULES: dict[str, RiskRule] = {r.id: r for r in (
    _R("limit_shock", "limit", "urgent", "Common-shock budget used", ra.FIRE_U, ra.CLEAR_U, "x"),
    _R("stress_breach", "limit", "urgent", "Worst stress-ladder loss vs the book loss limit", 1.0, None, "x"),
    _R("es", "es", "urgent", "Book expected shortfall vs its limit", ra.FIRE_U, ra.CLEAR_U, "x"),
    _R("margin", "margin", "urgent", "Margin cap used", ra.FIRE_U, ra.CLEAR_U, "x"),
    _R("margin_stress", "margin", "urgent", "Stressed margin above the funds available", 1.0, None, "x"),
    _R("gap_stop", "gap_stop", "urgent", "Chance one overnight gap jumps past a short strike", ra.GAP_STOP_LEVEL,
       None, "%"),
    _R("iv_shock", "shock", "warn", "IV30 shock (one session)", ra.SHOCK_Z, None, "x"),
    _R("cum_shock", "shock", "warn", "IV30 cumulative shock (5 sessions)", ra.SHOCK_Z, None, "x"),
    _R("gap", "gap", "warn", "Overnight gap", ra.GAP_Z, None, "x"),
    _R("touch", "touch", "warn", "Short strike's risk-neutral touch probability", ra.TOUCH_LEVEL, None, "%"),
    _R("event", "event", "info", "Tier-1 event inside an index position's holding window", 1.0, None, ""),
)}  # fmt: skip


@dataclass(frozen=True)
class Reading:
    value: float | None  # None = unknown
    series: str  # the series read, e.g. "fno_iv_eod.iv_30d NIFTY to 2026-10-21"
    detail: str  # the inputs, or why the value is unknown


def _sig(r: RiskRule) -> str:
    return hashlib.sha256(json.dumps(asdict(r), sort_keys=True).encode()).hexdigest()[:16]


def _fmt(r: RiskRule, x: float) -> str:
    return f"{x:.0%}" if r.unit == "%" else (f"{x:.2f}" if r.unit == "x" else ("yes" if x >= 1 else "no"))


def _state(s: Session, r: RiskRule, inst: str) -> AlertRuleState:
    rid = PREFIX + r.id
    st = s.scalars(select(AlertRuleState).where(AlertRuleState.rule_id == rid,
                                                AlertRuleState.instrument == inst)).first()  # fmt: skip
    if st is None or st.rule_sig != _sig(r):
        if st is None:
            st = AlertRuleState(rule_id=rid, instrument=inst)
            s.add(st)
        st.rule_sig, st.status, st.baseline, st.value, st.note, st.since = (
            _sig(r),
            "new",
            {},
            None,
            None,
            None,
        )
        st.last_fired_at, st.fired_count, st.suppressed_count = None, 0, 0
    return st


def _record(s: Session, r: RiskRule, inst: str, rd: Reading, status: str, now: datetime) -> Alert:
    from finresearch.monitor import notify

    who = "the F&O book" if inst == "BOOK" else inst
    if status == "unknown":
        text = f"{r.label} ({who}): can't check, the input is unknown: {rd.detail}. Series: {rd.series}."
        level, push = "warn", ("high" if r.tier == "urgent" else None)
    else:
        bound = f"clears below {_fmt(r, r.clear)}" if r.clear is not None else "registered level"
        text = (f"{r.label} ({who}): {_fmt(r, rd.value)} >= {_fmt(r, r.fire)} ({bound}); {rd.detail}. "  # type: ignore[arg-type]
                f"Series: {rd.series}. An alert only: nothing was traded.")  # fmt: skip
        level, push = LEVEL[r.tier], ("urgent" if r.tier == "urgent" else None)
    a = Alert(kind=RISK_KIND, level=level, message=text,
              data={"rule": r.id, "category": r.category, "tier": r.tier, "status": status, "instrument": inst,
                    "value": None if rd.value is None else round(rd.value, 6), "threshold": r.fire, "clear": r.clear,
                    "series": rd.series, "detail": rd.detail, "path": r.path})  # fmt: skip
    s.add(a)
    s.flush()
    if push:
        notify.queue(s, a, list(notify.CHANNELS), push, title=f"FinResearch · {r.label}", message=text, path=r.path,
                     now=now)  # fmt: skip
    return a


def apply(s: Session, r: RiskRule, inst: str, rd: Reading, now: datetime) -> str:
    """Update one rule's state with a reading; returns fired | still_fired | clear | unknown | still_unknown."""
    st = _state(s, r, inst)
    st.checked_at, st.source = now, rd.series
    if rd.value is None:
        st.note = rd.detail[:500]
        if st.status == "unknown":
            return "still_unknown"
        st.status, st.since = "unknown", now
        _record(s, r, inst, rd, "unknown", now)
        return "unknown"
    st.value, st.note = Decimal(str(round(rd.value, 6))), None
    on = (
        ra.hysteresis(st.status, rd.value, r.fire, r.clear) == "fired"
        if r.clear is not None
        else rd.value >= r.fire
    )
    if not on:
        if st.status != "clear":
            st.status, st.since = "clear", now
        return "clear"
    if st.status == "fired":
        return "still_fired"
    st.status, st.since, st.last_fired_at = "fired", now, now
    st.fired_count += 1
    _record(s, r, inst, rd, "fired", now)
    return "fired"

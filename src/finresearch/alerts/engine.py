"""Evaluate alert rules and record alerts: fire once when a condition becomes true, not again until it clears.

State per (rule, instrument) lives in `alert_rule_state`:
- new/clear/unknown -> the condition is true: the rule fires (an Alert plus a delivery per chosen channel), unless it
  last fired less than `cooldown_h` ago (then it is marked fired but "suppressed": no alert, counted);
- fired -> still true: nothing (de-duplication);
- fired -> false: clear (the next true fires again, after the cooldown);
- value unknown (the data source failed or has no data): nothing changes and nothing fires.
Editing a rule's definition resets its state (a digest of the definition is stored).

`rules_step` runs from the monitor tick: an intraday pass every 15 minutes while NSE's cash market is open
(09:15-15:30 IST, trading days) and a daily pass once each trading day from the profile's after-close check time.
Each pass is claimed in `alert_eval_slot`, so two monitor processes never run the same pass.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.alerts.compute import Reader, Reading
from finresearch.alerts.registry import OP_PHRASE, MetricSpec, fmt_value, sentence, spec
from finresearch.db.models import Alert, AlertEvalSlot, AlertRuleState, Company, Watch
from finresearch.suggest.profile import AlertRule
from finresearch.suggest.rules import OPS

log = logging.getLogger(__name__)

ALERT_KIND = "rule_alert"
LEVEL_FOR_PRIORITY = {"urgent": "action", "high": "action", "default": "warn", "low": "info", "min": "info"}
MARKET_OPEN, MARKET_LAST_PASS = time(9, 15), time(15, 45)  # the 15:30 pass records the close
INTRADAY_STEP_MIN = 15


@dataclass
class Target:
    instrument: str
    watch_id: int | None = None
    label: str | None = None


def rule_sig(r: AlertRule) -> str:
    body = json.dumps({"k": r.kind, "m": r.metric, "o": r.op, "v": str(r.value), "i": r.instrument,
                       "p": r.params}, sort_keys=True)  # fmt: skip
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def app_path(kind: str, instrument: str, watch_id: int | None) -> str:
    """The app page an alert links to."""
    if kind == "ipo":
        return f"/monitor/{watch_id}" if watch_id else "/monitor"
    return {"stock": f"/stocks/{instrument}", "fund": f"/funds/{instrument}", "bond": f"/bonds/{instrument}",
            "fno": f"/fno?symbol={instrument}", "portfolio": "/portfolio"}.get(kind, "/monitor")  # fmt: skip


def targets(session: Session, r: AlertRule) -> list[Target]:
    """The instruments a rule applies to: its own, or every watch of its kind."""
    if r.kind == "portfolio":
        return [Target("PORTFOLIO")]
    if r.kind in ("ipo", "stock"):
        q = select(Watch).where(Watch.kind == r.kind)
        watches = session.scalars(q).all()
        if r.instrument:
            w = next((w for w in watches if w.key == r.instrument or w.nse_symbol == r.instrument), None)
            return [Target(r.instrument, w.id if w else None, w.label if w else None)]
        return [Target(w.key, w.id, w.label) for w in watches if w.active and w.key]
    if r.instrument:
        return [Target(r.instrument)]
    prefix = {"fund": "mf-", "bond": "bond-"}.get(r.kind)
    if prefix is None:
        return []
    slugs = session.scalars(select(Company.slug).where(Company.slug.like(f"{prefix}%"))).all()
    return [Target(s[len(prefix) :].upper()) for s in sorted(slugs)]


def _state(session: Session, rule: AlertRule, inst: str) -> AlertRuleState:
    st = session.scalars(select(AlertRuleState).where(AlertRuleState.rule_id == rule.id,
                                                      AlertRuleState.instrument == inst)).first()  # fmt: skip
    sig = rule_sig(rule)
    if st is None:
        st = AlertRuleState(rule_id=rule.id, instrument=inst, rule_sig=sig, status="new", baseline={},
                            fired_count=0, suppressed_count=0)  # fmt: skip
        session.add(st)
    elif st.rule_sig != sig:  # the rule was edited: start afresh
        st.rule_sig, st.status, st.baseline, st.value, st.note = sig, "new", {}, None, None
        st.last_fired_at, st.since, st.fired_count, st.suppressed_count = None, None, 0, 0
    return st


def message(rule: AlertRule, m: MetricSpec, t: Target, reading: Reading) -> str:
    who = t.label or t.instrument
    if rule.kind == "portfolio":
        who = "Portfolio"
    if m.event:
        text = f"{who}: {m.event_text}"
        return text + (f" ({reading.detail})" if reading.detail else "")
    text = (f"{who}: {m.phrase} is {fmt_value(m.unit, reading.value)} "
            f"({OP_PHRASE[rule.op]} your {fmt_value(m.unit, rule.value)})")  # fmt: skip
    return text + (f"; {reading.detail}" if reading.detail else "")


def apply(session: Session, rule: AlertRule, t: Target, reading: Reading, now: datetime) -> str:
    """Update the rule's state with a fresh reading; record an alert when it fires. Returns what happened:
    fired | suppressed | still_fired | clear | unknown."""
    from finresearch.monitor import notify

    m = spec(rule.kind, rule.metric)
    st = _state(session, rule, t.instrument)
    st.checked_at = now
    if reading.baseline is not None and (m.rebase == "always" or not st.baseline):
        st.baseline = reading.baseline
    if reading.value is None:
        st.note, st.source = reading.note, reading.source
        if st.status == "new":
            st.status = "unknown"
        return "unknown"
    st.value, st.source, st.note = reading.value, reading.source, None
    if not OPS[rule.op](reading.value, rule.value):
        if st.status != "clear":
            st.status, st.since = "clear", now
        return "clear"
    if st.status == "fired":
        return "still_fired"
    st.status, st.since = "fired", now
    if st.last_fired_at is not None and now - st.last_fired_at < timedelta(hours=float(rule.cooldown_h)):
        st.suppressed_count += 1
        st.note = f"fired again within the {rule.cooldown_h:g} h cooldown; not re-sent"
        return "suppressed"
    st.last_fired_at = now
    st.fired_count += 1
    if m.rebase == "on_fire" and reading.baseline is not None:
        st.baseline = reading.baseline
    text = message(rule, m, t, reading)
    path = app_path(rule.kind, t.instrument, t.watch_id)
    a = Alert(watch_id=t.watch_id, kind=ALERT_KIND, level=LEVEL_FOR_PRIORITY[rule.priority], message=text,
              data={"rule_id": rule.id, "rule_kind": rule.kind, "metric": rule.metric, "instrument": t.instrument,
                    "op": rule.op, "threshold": str(rule.value), "value": str(reading.value),
                    "unit": m.unit, "source": reading.source, "as_of": reading.as_of, "priority": rule.priority,
                    "rule": rule.description or sentence(rule.kind, rule.metric, rule.op, rule.value, rule.instrument),
                    "path": path})  # fmt: skip
    session.add(a)
    session.flush()
    notify.queue(session, a, rule.channels, rule.priority, title=f"FinResearch · {m.label}", message=text,
                 path=path, now=now)  # fmt: skip
    return "fired"


async def evaluate(
    session: Session, rules: list[AlertRule], now: datetime, *, cadence: str | None = None
) -> dict:
    """Evaluate `rules` (only metrics of `cadence`, when given) for all their targets. Returns counts and details."""
    reader = Reader(session)
    out: dict[str, Any] = {
        "fired": 0,
        "suppressed": 0,
        "clear": 0,
        "unknown": 0,
        "still_fired": 0,
        "checks": [],
    }
    for rule in rules:
        m = spec(rule.kind, rule.metric)
        if m is None or not rule.enabled or (cadence and m.cadence != cadence):
            continue
        for t in targets(session, rule):
            st = session.scalars(select(AlertRuleState).where(AlertRuleState.rule_id == rule.id,
                                                              AlertRuleState.instrument == t.instrument)).first()  # fmt: skip
            baseline = dict(st.baseline) if st and st.rule_sig == rule_sig(rule) else {}
            reading = await reader.read(rule.kind, rule.metric, t.instrument, rule.params, baseline)
            what = apply(session, rule, t, reading, now)
            out[what] += 1
            out["checks"].append({"rule_id": rule.id, "instrument": t.instrument, "result": what,
                                  "value": None if reading.value is None else str(reading.value),
                                  "note": reading.note, "source": reading.source})  # fmt: skip
    return out


def prune_states(session: Session, rules: list[AlertRule]) -> int:
    """Forget the state of rules that no longer exist."""
    ids = [r.id for r in rules]
    q = delete(AlertRuleState)
    if ids:
        q = q.where(AlertRuleState.rule_id.not_in(ids))
    return session.execute(q).rowcount or 0


def due_passes(
    now: datetime, daily_at: tuple[int, int], holidays: set | None = None
) -> list[tuple[str, str]]:
    """The passes due at `now`: [("intraday", slot), ("daily", slot)] as applicable (IST, trading days only)."""
    from finresearch.fincalc.dates import is_business_day, to_ist

    ist = to_ist(now)
    day = ist.date()
    if holidays is None:
        from finresearch.adapters.nse_holidays import trading_holidays

        holidays = trading_holidays()
    if not is_business_day(day, holidays):
        return []
    out = []
    t = ist.time()
    if MARKET_OPEN <= t < MARKET_LAST_PASS:
        minutes = (t.hour * 60 + t.minute) // INTRADAY_STEP_MIN * INTRADAY_STEP_MIN
        slot = max(minutes, MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
        out.append(("intraday", f"intraday:{day.isoformat()}T{slot // 60:02d}:{slot % 60:02d}"))
    if t >= time(*daily_at):
        out.append(("daily", f"daily:{day.isoformat()}"))
    return out


def _claim(slot: str, now: datetime) -> bool:
    from finresearch.db import session_scope

    with session_scope() as s:
        got = s.execute(insert(AlertEvalSlot).values(slot=slot, started_at=now, result={})
                        .on_conflict_do_nothing(index_elements=["slot"]).returning(AlertEvalSlot.slot)).first()  # fmt: skip
    return got is not None


async def rules_step(now: datetime, *, holidays: set | None = None) -> dict[str, Any]:
    """The monitor's alert-rule work for this tick (see the module docstring). No rules = no network, no rows."""
    from finresearch.db import session_scope
    from finresearch.suggest.advisor import load_profile

    with session_scope() as s:
        profile = load_profile(s)
        rules = [r for r in profile.alert_rules if r.enabled]
        prune_states(s, profile.alert_rules)
    if not rules:
        return {}
    out: dict[str, Any] = {}
    for cadence, slot in due_passes(now, profile.preferences.watch.stock_time(), holidays):
        mine = [r for r in rules if (m := spec(r.kind, r.metric)) and m.cadence == cadence]
        if not mine or not _claim(slot, now):
            continue
        try:
            with session_scope() as s:
                res = await evaluate(s, mine, now, cadence=cadence)
                row = s.get(AlertEvalSlot, slot)
                if row is not None:
                    row.result = {k: v for k, v in res.items() if k != "checks"} | {
                        "checks": res["checks"][:200]
                    }
        except Exception:
            with (
                session_scope() as s
            ):  # release the pass so the next tick retries it (a daily pass is not lost)
                s.execute(delete(AlertEvalSlot).where(AlertEvalSlot.slot == slot))
            raise
        out[cadence] = {k: res[k] for k in ("fired", "suppressed", "unknown")}
    return out


def state_json(st: AlertRuleState) -> dict[str, Any]:
    def iso(d: datetime | None) -> str | None:
        return d.isoformat() if d else None

    return {"rule_id": st.rule_id, "instrument": st.instrument, "status": st.status,
            "value": None if st.value is None else format(Decimal(st.value).normalize(), "f"), "source": st.source,
            "note": st.note, "since": iso(st.since), "last_fired_at": iso(st.last_fired_at),
            "fired_count": st.fired_count, "suppressed_count": st.suppressed_count,
            "checked_at": iso(st.checked_at)}  # fmt: skip

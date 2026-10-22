"""P4.5 (#398): does an alert lead to action? One tap on an alert records "acted" or "ignored" (in its data, through
a POST); per rule, precision = actioned / fired, and an informational rule with precision below MUTE_PRECISION after
at least MUTE_MIN_FIRED firings is suggested for muting (fincalc.risk_alerts.suggest_mute). Limit, margin, ES and
gap-through-stop rules are never suggested. A "can't check" alert is not a firing. An alert nobody answered counts
as not actioned, as the issue's formula has it (the answered count is shown beside it). Reads only."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import Alert
from finresearch.fincalc import risk_alerts as ra

OUTCOMES = ("acted", "ignored")
RULE_TEXT = (f"precision = acted / fired; an informational rule below {ra.MUTE_PRECISION:.0%} after at least "
             f"{ra.MUTE_MIN_FIRED} firings is suggested for muting. Limit, margin, ES and gap-through-stop alerts are "
             "never suggested.")  # fmt: skip


def feedback(s: Session, alert_id: int, outcome: str, now: datetime) -> Alert:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    a = s.get(Alert, alert_id)
    if a is None:
        raise LookupError(f"unknown alert {alert_id}")
    a.data = {**(a.data or {}), "feedback": outcome, "feedback_at": now.isoformat()}
    a.read_at = a.read_at or now
    return a


def _key(a: Alert) -> tuple[str, str, str, str] | None:
    """(rule, label, category, tier) of a firing alert, or None."""
    from finresearch.alerts.risk import RISK_KIND, RULES

    d = a.data or {}
    if a.kind == RISK_KIND and d.get("status") == "fired" and d.get("rule") in RULES:
        r = RULES[d["rule"]]
        return r.id, r.label, r.category, r.tier
    if a.kind == "rule_alert" and d.get("rule_id"):
        return (
            f"rule:{d['rule_id']}",
            str(d.get("rule") or d["rule_id"]),
            "rule",
            str(d.get("priority") or ""),
        )
    return None


def by_rule(s: Session) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    count: dict[str, defaultdict[str, int]] = {}
    for a in s.scalars(select(Alert).where(Alert.kind.in_(("risk_alert", "rule_alert"))).order_by(Alert.id)):
        k = _key(a)
        if k is None:
            continue
        agg.setdefault(k[0], {"rule": k[0], "label": k[1], "category": k[2], "tier": k[3]})
        c = count.setdefault(k[0], defaultdict(int))
        c["fired"] += 1
        c[(a.data or {}).get("feedback") or "unanswered"] += 1
    out = []
    for rid, row in agg.items():
        c = count[rid]
        out.append({**row, "fired": c["fired"], "acted": c["acted"], "ignored": c["ignored"],
                    "unanswered": c["unanswered"], "precision": ra.precision(c["acted"], c["fired"]),
                    "never_mute": row["category"] in ra.NEVER_MUTE,
                    "suggest_mute": ra.suggest_mute(row["category"], fired=c["fired"], actioned=c["acted"])})  # fmt: skip
    return sorted(out, key=lambda r: (-r["fired"], r["rule"]))

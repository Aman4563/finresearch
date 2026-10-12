"""Exit rules on F&O positions, real or paper (#308): they raise one alert each when they trigger, never trade.

Kinds and readings (a reading that cannot be made is unknown: the rule neither fires nor clears):
* profit: gross P&L ÷ the entry net premium (debit paid, or credit received), %, fires at ≥ threshold;
* stop: the same ratio, fires at ≤ −threshold;
* dte: calendar days to expiry, fires at ≤ threshold;
* delta: the largest |delta| of the short option legs (Black-Scholes at the chain's IV for the strike, else the IV
  implied by the leg's last price), fires at ≥ threshold (e.g. 0.30);
* event: days until the next results date or RBI policy date on or before expiry, fires at ≤ threshold.
The P&L base is the option premium only: a futures-only position has none, so its profit and stop rules stay unknown.

A rule fires once: it records an `Alert` (kind "fno_exit", level "action"; the alerts engine forwards monitor alerts
to the notification channels chosen in Settings) and becomes "fired" for good. Rules are never deleted with their
position; a closed paper trade's rules simply stop being checked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finresearch.db.models import Alert, FnoExitRule

KINDS = ("profit", "stop", "dte", "delta", "event")
ALERT_KIND = "fno_exit"
TRIGGER, CLEAR, UNKNOWN = "trigger", "clear", "unknown"


@dataclass
class Snapshot:
    """One position (a paper strategy, or a real underlying + expiry group) as the rules read it."""

    label: str
    symbol: str
    expiry: date
    today: date
    gross_pnl: float | None = None
    base: float | None = None  # |entry net option premium|, ₹
    short_deltas: list[float | None] = field(default_factory=list)  # one per short option leg; None = unknown
    events: list[tuple[date, str]] | None = None  # None = the event sources could not be read
    path: str = "/fno"


@dataclass(frozen=True)
class Reading:
    value: float | None
    status: str  # trigger | clear | unknown
    detail: str


def read(kind: str, threshold: float, p: Snapshot) -> Reading:
    if kind in ("profit", "stop"):
        if p.gross_pnl is None:
            return Reading(None, UNKNOWN, "no mark-to-market price yet")
        if not p.base:
            return Reading(None, UNKNOWN, "no option premium to measure a % P&L against")
        pct = p.gross_pnl / p.base * 100
        hit = pct >= threshold if kind == "profit" else pct <= -threshold
        return Reading(pct, TRIGGER if hit else CLEAR, f"P&L {pct:+.1f}% of the entry premium ₹{p.base:,.0f}")
    if kind == "dte":
        d = (p.expiry - p.today).days
        return Reading(d, TRIGGER if d <= threshold else CLEAR, f"{d} days to expiry")
    if kind == "delta":
        if not p.short_deltas:
            return Reading(None, UNKNOWN, "no short option legs")
        if any(x is None for x in p.short_deltas):
            return Reading(None, UNKNOWN, "a short leg's delta is unknown (no IV)")
        worst = max(abs(x) for x in p.short_deltas)  # type: ignore[arg-type]
        return Reading(worst, TRIGGER if worst >= threshold else CLEAR, f"short-strike |delta| {worst:.2f}")
    if kind == "event":
        if p.events is None:
            return Reading(None, UNKNOWN, "the event calendar could not be read")
        ahead = sorted((d, what) for d, what in p.events if p.today <= d <= p.expiry)
        if not ahead:
            return Reading(None, CLEAR, "no known event before expiry")
        d, what = ahead[0]
        days = (d - p.today).days
        return Reading(
            days, TRIGGER if days <= threshold else CLEAR, f"{what} on {d:%d-%b-%Y}, before expiry"
        )
    raise ValueError(f"unknown exit rule kind {kind!r}")


def _phrase(r: FnoExitRule) -> str:
    t = float(r.threshold)
    return {"profit": f"profit target {t:g}%", "stop": f"stop {t:g}%", "dte": f"{t:g} days to expiry",
            "delta": f"short-strike delta {t:g}", "event": f"event within {t:g} days"}[r.kind]  # fmt: skip


def apply(s: Session, rule: FnoExitRule, p: Snapshot, now: datetime) -> str:
    """Check one rule: returns fired | still_fired | clear | unknown. A fired rule is only re-read, never re-fired."""
    rd = read(rule.kind, float(rule.threshold), p)
    rule.checked_at, rule.note = now, rd.detail
    rule.last_value = None if rd.value is None else Decimal(str(round(rd.value, 4)))
    if rule.status == "fired":
        return "still_fired"
    if rd.status != TRIGGER:
        return rd.status
    text = (
        f"{p.label}: exit rule hit ({_phrase(rule)}): {rd.detail}. Review the position; nothing was traded."
    )
    a = Alert(kind=ALERT_KIND, level="action", message=text,
              data={"rule_id": rule.id, "kind": rule.kind, "threshold": str(rule.threshold), "value": rd.value,
                    "paper_id": rule.paper_id, "position_key": rule.position_key, "symbol": p.symbol,
                    "expiry": p.expiry.isoformat(), "path": p.path})  # fmt: skip
    s.add(a)
    s.flush()
    rule.status, rule.fired_at, rule.alert_id = "fired", now, a.id
    return "fired"


def run(
    s: Session, rules: Sequence[FnoExitRule], snaps: dict[str, Snapshot], now: datetime
) -> dict[str, int]:
    """Check `rules` against the snapshots keyed "paper:<id>" / "real:<position key>"; a rule without a snapshot (its
    position is closed or gone) is skipped."""
    out: dict[str, int] = {}
    for r in rules:
        p = snaps.get(target_key(r))
        if p is None:
            continue
        what = apply(s, r, p, now)
        out[what] = out.get(what, 0) + 1
    return out


def target_key(r: FnoExitRule) -> str:
    return f"paper:{r.paper_id}" if r.paper_id is not None else f"real:{r.position_key}"


def rule_json(r: FnoExitRule) -> dict[str, Any]:
    return {"id": r.id, "paper_id": r.paper_id, "position_key": r.position_key, "kind": r.kind,
            "threshold": float(r.threshold), "status": r.status, "phrase": _phrase(r),
            "last_value": None if r.last_value is None else float(r.last_value), "note": r.note,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
            "fired_at": r.fired_at.isoformat() if r.fired_at else None, "alert_id": r.alert_id}  # fmt: skip

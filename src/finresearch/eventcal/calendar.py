"""Pure point-in-time rules of the event calendar (#367), no database.

* Point in time: an event (or a coverage statement) is usable at time t only if known_since <= t (and, for a date
  later moved, t < withdrawn_since), so a backtest replaying day t never sees a meeting announced after t.
* Coverage: an empty answer is "no events" only for event types whose schedule is known through the end of the window
  (a `Coverage` row known at t); any tier-1 type without such coverage makes the check "unknown", never "none".
* India session: an event released after NSE's 15:30 IST close (FOMC at 14:00 ET, US CPI at 08:30 ET) moves Indian
  prices on the next calendar day; the holding-window check compares on that day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from finresearch.fincalc.dates import IST

# Tier 1 (registered, T17): the events whose gap risk the plan check reports. Everything else is context.
TIER1_TYPES: tuple[str, ...] = ("rbi_mpc", "union_budget", "general_election")
TITLES = {
    "rbi_mpc": "RBI monetary policy decision",
    "union_budget": "Union Budget",
    "general_election": "General-election results (Lok Sabha)",
    "fomc": "US FOMC decision",
    "us_cpi": "US CPI release",
    "india_cpi": "India CPI release (MoSPI)",  # context (T17), from the nightly refresh (eventcal.mospi)
    "india_gdp": "India GDP estimates release (MoSPI)",
}
NSE_CLOSE = time(15, 30)


@dataclass(frozen=True)
class Event:
    type: str
    tier: int  # 1 = registered tier-1, 2 = context
    scope: str  # IN = India market-wide, US = US macro (context)
    day: date  # the event's own (local) day
    at: datetime | None  # aware release time when the source states it
    title: str
    source_url: str
    known_since: datetime  # aware: when the schedule announcing this event was public
    known_since_url: str | None = None
    # a rescheduled or cancelled date: unusable from this time on (the notice that moved it); None = still standing
    withdrawn_since: datetime | None = None


@dataclass(frozen=True)
class Coverage:
    """`type` events in `scope` are completely listed through `through`, as stated by `source_url` at known_since."""

    type: str
    scope: str
    through: date
    source_url: str
    known_since: datetime
    note: str | None = None


def known_at(rows: list[Any], t: datetime) -> list[Any]:
    """The rows (events or coverage) public at `t`: known_since <= t, and not yet withdrawn (a moved meeting's old
    date stands until the notice that moved it)."""
    return [r for r in rows if r.known_since <= t and (getattr(r, "withdrawn_since", None) or datetime.max.replace(
        tzinfo=IST)) > t]  # fmt: skip


def india_day(e: Event) -> date:
    """The Indian session the event can first move: its IST day, or the next day when released after the close."""
    if e.at is None:
        return e.day
    ist = e.at.astimezone(IST)
    return ist.date() + timedelta(days=1) if ist.time() >= NSE_CLOSE else ist.date()


def covered_through(cov: list[Coverage], kind: str, t: datetime) -> date | None:
    got = [c.through for c in known_at(cov, t) if c.type == kind]
    return max(got) if got else None


def _row(e: Event) -> dict[str, Any]:
    return {"type": e.type, "title": e.title, "day": e.day.isoformat(), "india_day": india_day(e).isoformat(),
            "at": e.at.isoformat() if e.at else None, "source": e.source_url,
            "known_since": e.known_since.isoformat()}  # fmt: skip


def window_check(events: list[Event] | None, coverage: list[Coverage] | None, *, start: date, end: date,
                 as_of: datetime) -> dict[str, Any]:  # fmt: skip
    """The informational "tier-1 event inside holding window" check for [start, end] as known at `as_of`.
    status: events (a tier-1 event falls in the window), none (none does and every tier-1 schedule is known through
    `end`), unknown (otherwise; `uncovered` names the types). `context` lists context events in the window.
    None for `events`/`coverage` = the calendar could not be read: unknown."""
    if events is None or coverage is None:
        return {"status": "unknown", "tier1": [], "context": [], "uncovered": list(TIER1_TYPES),
                "note": "The event calendar could not be read: check RBI, Budget and election dates yourself."}  # fmt: skip
    seen = sorted(known_at(events, as_of), key=lambda e: (india_day(e), e.type))
    inside = [e for e in seen if start <= india_day(e) <= end]
    tier1 = [_row(e) for e in inside if e.tier == 1]
    context = [_row(e) for e in inside if e.tier != 1]
    uncovered = [k for k in TIER1_TYPES if (covered_through(coverage, k, as_of) or date.min) < end]
    status = "events" if tier1 else ("unknown" if uncovered else "none")
    note = {"events": "A tier-1 event falls inside the holding window: a gap can jump past any stop.",
            "none": "No tier-1 event (RBI policy, Union Budget, election results) inside the holding window.",
            "unknown": "The calendar does not cover the whole window for: " + ", ".join(TITLES[k] for k in uncovered)
            + ". Check those dates yourself."}[status]  # fmt: skip
    if tier1 and uncovered:
        note += " Not covered: " + ", ".join(TITLES[k] for k in uncovered) + "."
    return {"status": status, "tier1": tier1, "context": context, "uncovered": uncovered, "note": note}

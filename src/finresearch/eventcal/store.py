"""The event calendar's tables (#367): calendar_event and calendar_coverage, read back as eventcal.calendar rows."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import CalendarCoverage, CalendarEvent
from finresearch.eventcal.calendar import Coverage, Event


def upsert(s: Session, events: list[Event], coverage: list[Coverage]) -> tuple[int, int]:
    """Store events and coverage statements; returns (new events, new coverage rows). An event seen again keeps the
    earliest known_since (when it was first public never moves later); its title, time and source are refreshed."""
    have = {(r.type, r.scope, r.day): r for r in s.scalars(select(CalendarEvent))}
    new_e = 0
    for e in events:
        r = have.get((e.type, e.scope, e.day))
        if r is None:
            r = CalendarEvent(type=e.type, tier=e.tier, scope=e.scope, day=e.day, at=e.at, title=e.title,
                              source_url=e.source_url, known_since=e.known_since, known_since_url=e.known_since_url,
                              withdrawn_since=e.withdrawn_since)  # fmt: skip
            s.add(r)
            have[(e.type, e.scope, e.day)] = r
            new_e += 1
            continue
        r.tier, r.at, r.title, r.source_url = e.tier, e.at, e.title, e.source_url
        if e.withdrawn_since is not None and (
            r.withdrawn_since is None or e.withdrawn_since < r.withdrawn_since
        ):
            r.withdrawn_since = e.withdrawn_since
        if e.known_since < r.known_since:
            r.known_since, r.known_since_url = e.known_since, e.known_since_url
    seen = {(c.type, c.scope, c.through, c.source_url) for c in s.scalars(select(CalendarCoverage))}
    new_c = 0
    for c in coverage:
        if (c.type, c.scope, c.through, c.source_url) in seen:
            continue
        s.add(CalendarCoverage(type=c.type, scope=c.scope, through=c.through, source_url=c.source_url,
                               known_since=c.known_since, note=c.note))  # fmt: skip
        seen.add((c.type, c.scope, c.through, c.source_url))
        new_c += 1
    s.flush()
    return new_e, new_c


def load(s: Session) -> tuple[list[Event], list[Coverage]]:
    """Every stored event and coverage statement (callers filter by known_since with calendar.known_at)."""
    ev = [Event(type=r.type, tier=r.tier, scope=r.scope, day=r.day, at=r.at, title=r.title, source_url=r.source_url,
                known_since=r.known_since, known_since_url=r.known_since_url, withdrawn_since=r.withdrawn_since)
          for r in s.scalars(select(CalendarEvent).order_by(CalendarEvent.day, CalendarEvent.type))]  # fmt: skip
    cov = [Coverage(type=r.type, scope=r.scope, through=r.through, source_url=r.source_url, known_since=r.known_since,
                    note=r.note) for r in s.scalars(select(CalendarCoverage).order_by(CalendarCoverage.id))]  # fmt: skip
    return ev, cov

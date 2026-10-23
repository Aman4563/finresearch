"""The event calendar's nightly forward refresh (#367): each official source is read whole or not at all, and what it
read is merged into calendar_event / calendar_coverage under three rules.

* Never back-dated: every row a refresh adds is known from the time it was fetched (`now`), whatever date the
  document carries, so a backtest replaying an earlier day never sees it. A row already known earlier (the seed's,
  read off the same page by hand) keeps its earlier known_since (store.upsert).
* A moved date keeps its old row until the notice is seen: the old row is withdrawn at `now`, the time the new row
  becomes known, so at no time is neither date usable.
* A failed or refused source stores nothing, coverage included: past the last coverage the plan check keeps reading
  "unknown", never "no events". A source runs only with the owner's data-rights decision (data_rights.require).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.eventcal.calendar import Coverage, Event

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Move:
    """A notice moved the `type` event in `scope` from `old` to `new` (the new row is in Found.events)."""

    type: str
    scope: str
    old: date
    new: date


@dataclass(frozen=True)
class Replace:
    """A complete newer release calendar: stored `type` rows dated after `after` through `through` that it no longer
    lists are withdrawn (their date moved)."""

    type: str
    scope: str
    after: date
    through: date


@dataclass
class Found:
    """What one source read completely. known_since on these rows is ignored: `apply` stamps the fetch time."""

    dataset: str
    events: list[Event] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    replaces: list[Replace] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)  # what was read, for the log


@dataclass(frozen=True)
class Known:
    """What the calendar already holds, so a source can skip documents it has read and widen a catch-up search."""

    urls: frozenset[str]
    covered: dict[str, date]  # type -> the furthest coverage `through`


Reader = Callable[[Known, datetime], Awaitable[Found]]


@dataclass(frozen=True)
class Source:
    dataset: str  # the data_rights key
    read: Reader


def known(s: Session) -> Known:
    from finresearch.db.models import CalendarCoverage, CalendarEvent

    urls: set[str] = set()
    for a, b in s.execute(select(CalendarEvent.source_url, CalendarEvent.known_since_url)):
        urls |= {u for u in (a, b) if u}
    covered: dict[str, date] = {}
    for kind, through, url in s.execute(
        select(CalendarCoverage.type, CalendarCoverage.through, CalendarCoverage.source_url)
    ):
        urls.add(url)
        covered[kind] = max(covered.get(kind, through), through)
    return Known(urls=frozenset(urls), covered=covered)


def _withdraw(s: Session, kind: str, scope: str, now: datetime, keep: set[date], lo: date, hi: date) -> int:
    from finresearch.db.models import CalendarEvent

    n = 0
    for r in s.scalars(select(CalendarEvent).where(CalendarEvent.type == kind, CalendarEvent.scope == scope,
                                                   CalendarEvent.day >= lo, CalendarEvent.day <= hi)):  # fmt: skip
        if r.day not in keep and (r.withdrawn_since is None or r.withdrawn_since > now):
            r.withdrawn_since = now
            n += 1
    return n


def apply(s: Session, found: Found, now: datetime) -> dict[str, int]:
    """Merge one source's complete read: rows known from `now`, then the moves and replacements."""
    from finresearch.eventcal import store

    events = [replace(e, known_since=now, known_since_url=e.known_since_url or e.source_url, withdrawn_since=None)
              for e in found.events]  # fmt: skip
    coverage = [replace(c, known_since=now) for c in found.coverage]
    ne, nc = store.upsert(s, events, coverage)
    withdrawn = 0
    for m in found.moves:
        withdrawn += _withdraw(s, m.type, m.scope, now, {m.new}, m.old, m.old)
    for r in found.replaces:
        keep = {e.day for e in events if e.type == r.type and e.scope == r.scope}
        withdrawn += _withdraw(
            s, r.type, r.scope, now, keep, date.fromordinal(r.after.toordinal() + 1), r.through
        )
    s.flush()
    return {"events": ne, "coverage": nc, "withdrawn": withdrawn}


async def run(sources: list[Source], now: datetime) -> dict[str, Any]:
    """Read every source the owner decided on and store each complete read in its own transaction."""
    from finresearch import data_rights
    from finresearch.db import session_scope

    out: dict[str, Any] = {"stored": {}, "skipped": {}, "failed": {}}
    for src in sources:
        try:
            data_rights.require(src.dataset)
        except data_rights.NoDecision as e:
            out["skipped"][src.dataset] = str(e)[:300]
            continue
        with session_scope() as s:
            have = known(s)
        try:
            found = await src.read(have, now)
        except Exception as e:
            log.warning("event calendar source %s failed; nothing stored", src.dataset, exc_info=True)
            out["failed"][src.dataset] = f"{type(e).__name__}: {e}"[:300]
            continue
        with session_scope() as s:
            out["stored"][src.dataset] = apply(s, found, now)
    return out

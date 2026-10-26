"""RBI policy-rate observations from RBI's press-release RSS feed (#492): the table policy_rate_obs, written by the
event calendar's RSS read (eventcal.refresh.apply, from eventcal.rbi.observe) and read by the risk-free chain
(signals.rates) and the data-quality pass (dq.policy). Nothing here requests anything.

Append-only. An observation is stored once, known from the read that first saw it (`known_since`); a later read of
the same item, decision and rates adds nothing, and no row is ever updated. A later item, or the same item's changed
text, giving other rates for an already observed decision is stored too, flagged in `conflict` with the earlier
item's link and both figures: it is never merged and never replaces the first.

Data rights: covered by `rbi_rss` (#481), facts from the sanctioned feed only: the decision date, the rates as
numbers, the item's link and title and the times; never the item's text.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from finresearch.db.models import PolicyRateObs as Row
from finresearch.fincalc.dates import to_ist
from finresearch.fincalc.policy_feed import Observation, Observed, differences, when

LOCK_KEY = 492_001  # pg_advisory_xact_lock: reads at once (monitor, `eventcal refresh`) store an item once


def _key(o: Observation) -> tuple[Any, ...]:
    return o.url, o.decided, o.repo, o.sdf, o.msf


def _dec(x: Decimal | None) -> Decimal | None:
    return None if x is None else Decimal(x).normalize()


def _ist(t: datetime | None) -> datetime | None:
    return None if t is None else to_ist(t)  # the session's zone is the server's (UTC in CI): shown in IST


def load(s: Session) -> list[Observation]:
    """Every stored observation, first seen first; times in IST."""
    rows = s.scalars(select(Row).order_by(Row.known_since, Row.id)).all()
    return [Observation(decided=r.decided, repo=_dec(r.repo), sdf=_dec(r.sdf), msf=_dec(r.msf), title=r.title,
                        url=r.url, published=_ist(r.published), seen=_ist(r.known_since), conflict=r.conflict)
            for r in rows]  # fmt: skip


def record(s: Session, observations: list[Observation], now: datetime) -> int:
    """Store the observations not stored yet, known from `now`; returns how many were added."""
    s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
    have = load(s)
    seen = {_key(o) for o in have}
    added = 0
    for o in observations:
        if _key(o) in seen:
            continue
        clash = next((h for h in have if h.decided == o.decided and differences(h, o)), None)
        note = None
        if clash is not None:
            note = (f"other rates than {clash.url} (seen {when(clash.seen)}): "
                    + "; ".join(differences(clash, o)))[:300]  # fmt: skip
        s.add(Row(decided=o.decided, repo=o.repo, sdf=o.sdf, msf=o.msf, title=o.title[:300], url=o.url[:400],
                  published=o.published, known_since=now, conflict=note))  # fmt: skip
        have.append(replace(o, seen=now, conflict=note))
        seen.add(_key(o))
        added += 1
    s.flush()
    return added


def observed(s: Session) -> Observed:
    """The stored observations with the event calendar's MPC schedule, for the rate chain: the standing (not
    withdrawn) rbi_mpc decision days and the furthest day the calendar's coverage lists every meeting through. Read
    from the calendar's tables: the rate chain does not import the event calendar's readers."""
    from sqlalchemy import func

    from finresearch.db.models import CalendarCoverage, CalendarEvent

    ev = CalendarEvent
    days = s.scalars(
        select(ev.day).where(ev.type == "rbi_mpc", ev.scope == "IN", ev.withdrawn_since.is_(None))
    )
    through = s.scalar(select(func.max(CalendarCoverage.through)).where(CalendarCoverage.type == "rbi_mpc",
                                                                        CalendarCoverage.scope == "IN"))  # fmt: skip
    return Observed(observations=tuple(load(s)), scheduled=tuple(sorted(set(days))), covered=through)

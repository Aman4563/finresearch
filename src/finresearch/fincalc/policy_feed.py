"""RBI's policy rates as RBI's press-release RSS feed states them (#492, the owner's request of 26-Oct-2026: no hand
curation), merged with the hand-typed table of fincalc.policy_rates. Pure: the caller passes the observations (stored
by refseries.policy_obs) and the MPC schedule (the event calendar's rows); nothing here fetches or reads a database.

Source and data rights: covered by `rbi_rss` (#481), facts from the sanctioned feed only. eventcal.rbi already reads
the feed for MPC dates; each MPC resolution item carries the release's text in its description, and the policy rates
it states are parsed in memory from that same read (eventcal.rbi.observe): no other request, no page fetched. Only
the parsed numbers, the decision date, the item's link and title and the times are kept, never the item's text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache

from finresearch.fincalc import policy_rates as P


@dataclass(frozen=True)
class Observation:
    """One MPC resolution as read from the feed. Rates are fractions (5.50 per cent = 0.055) exactly as printed;
    None when the text does not state one (unknown, never 0)."""

    decided: date  # the decision day: the meeting's last day, from the item's title
    repo: Decimal | None
    sdf: Decimal | None  # the standing deposit facility rate, the corridor's floor since 08-Apr-2022
    msf: Decimal | None  # the marginal standing facility rate (the Bank Rate moves with it)
    title: str  # the item's title, as printed
    url: str  # the item's link, in the citation form of fincalc.policy_rates
    published: datetime | None = None  # the item's pubDate (IST)
    seen: datetime | None = None  # when the app first read it (known_since); set when stored
    conflict: str | None = None  # set when stored: an earlier one for the same decision states other rates


RATES = (("repo", "repo"), ("sdf", "SDF"), ("msf", "MSF"))


def pct(x: Decimal | None) -> str:
    return "unknown" if x is None else f"{x * 100:.2f} %"


def when(t: datetime | None) -> str:
    from finresearch.fincalc.dates import to_ist

    return "at an unknown time" if t is None else f"{to_ist(t):%d-%b-%Y %H:%M} IST"


def differences(a: Observation, b: Observation) -> list[str]:
    """The rates both state with different figures, b's first: ['repo 6.00 % vs 5.75 %']. A rate one of them does
    not state is no difference (unknown is not a disagreement)."""
    out = []
    for name, label in RATES:
        x, y = getattr(a, name), getattr(b, name)
        if x is not None and y is not None and x != y:
            out.append(f"{label} {pct(y)} vs {pct(x)}")
    return out


@dataclass(frozen=True)
class Observed:
    """What RBI's RSS feed and the event calendar know, for the pure rate chain. The default (empty) is the hand-typed
    table alone, possibly stale from its P.NEXT_MPC as before #492."""

    observations: tuple[Observation, ...] = ()  # refseries.policy_obs.load: first seen first
    scheduled: tuple[date, ...] = ()  # the event calendar's standing rbi_mpc decision days
    covered: date | None = None  # every scheduled MPC meeting is listed through this day; None: unknown


@dataclass(frozen=True)
class Merged:
    rows: tuple[P.PolicyRate, ...]  # the table, then a row per decision read from the feed after P.REVIEWED
    # (decision day, note): another item gave other rates for a decision; the first one seen is used
    conflicts: tuple[tuple[date, str], ...]
    disagreements: tuple[str, ...]  # what the table contradicts on a day it covers (the table is kept)


def _first_seen(o: Observation) -> tuple[bool, float]:
    return o.seen is None, o.seen.timestamp() if o.seen else 0.0


def _against_table(o: Observation, row: P.PolicyRate) -> list[str]:
    floor = row.floor if row.floor_kind == "SDF" else None  # an SDF figure says nothing of a reverse repo
    pairs = (("repo", o.repo, row.repo), ("SDF", o.sdf, floor), ("MSF", o.msf, row.msf))
    return [f"{label} {pct(x)} where the table has {pct(y)}" for label, x, y in pairs
            if x is not None and y is not None and x != y]  # fmt: skip


def _released(o: Observation) -> date:
    from finresearch.fincalc.dates import to_ist

    return to_ist(o.published).date() if o.published else o.decided


def merge(observations: tuple[Observation, ...]) -> Merged:
    """The table extended by the feed. The table covers every day to P.covered_through() (REVIEWED: its rows and
    every MPC meeting between them were checked then) and wins there: an observation of such a day that contradicts
    it is listed in `disagreements`, never used. A later decision becomes a row from its day if an observation of it
    states the repo rate; the first one seen is used, and any other giving other rates for the same decision is listed
    in `conflicts` (an observation without the repo rate extends nothing: unknown)."""
    return _merge(observations, P.TABLE, P.covered_through())


@lru_cache(maxsize=64)
def _merge(observations: tuple[Observation, ...], table: tuple[P.PolicyRate, ...], covered: date) -> Merged:
    rows: list[P.PolicyRate] = list(table)
    conflicts: list[tuple[date, str]] = []
    disagreements: list[str] = []
    by_day: dict[date, list[Observation]] = {}
    for o in sorted(observations, key=_first_seen):
        by_day.setdefault(o.decided, []).append(o)
    for day in sorted(by_day):
        group = by_day[day]
        if day <= covered:
            row = P.in_force(day, rows=table)
            for o in group:
                if row is not None and (diff := _against_table(o, row)):
                    disagreements.append(f"RBI's RSS feed item {o.url} (seen {when(o.seen)}) states for the "
                                         f"{day:%d-%b-%Y} decision {'; '.join(diff)} (fincalc.policy_rates, kept)")  # fmt: skip
            continue
        used = next((o for o in group if o.repo is not None), None)
        if used is None:
            continue
        for o in group:
            if o is not used and (diff := differences(used, o)):
                conflicts.append((day, f"RBI's RSS feed gave other rates for the {day:%d-%b-%Y} decision in another "
                                       f"item ({o.url}, seen {when(o.seen)}: {'; '.join(diff)}); the first item seen "
                                       "is used, flagged in data quality"))  # fmt: skip
        rows.append(P.PolicyRate(effective=day, repo=used.repo, floor=used.sdf, floor_kind="SDF", msf=used.msf,
                                 title=used.title, released=_released(used), url=used.url, seen=used.seen))  # fmt: skip
    return Merged(tuple(rows), tuple(conflicts), tuple(disagreements))


def schedule(observed: Observed) -> tuple[list[date], date]:
    """The scheduled MPC decision days after the typed table's coverage (P.covered_through()), and the day through
    which that list is complete: the event calendar's when it covers the table's next meeting (P.NEXT_MPC), else that
    meeting alone. A day the typed table covers is never waited for (stale() and dq.policy agree on it)."""
    covered = P.covered_through()
    cal = {d for d in observed.scheduled if d > covered}
    if observed.covered is not None and observed.covered >= P.NEXT_MPC:
        return sorted(cal), max(observed.covered, covered)
    return sorted(d for d in {*cal, P.NEXT_MPC} if d > covered), max(P.NEXT_MPC, covered)


def stale(day: date, observed: Observed, *, before: bool = False) -> str | None:
    """Why an answer for `day` may miss a newer decision, or None. Stale when a scheduled decision day has come (with
    `before`, a day before `day`) later than the last decision known (the table's review, or a decision read from
    the feed with its repo rate), or when `day` is past the last day the schedule is known for."""
    known = [r.effective for r in merge(observed.observations).rows
             if r.seen is not None and (r.effective < day if before else r.effective <= day)]  # fmt: skip
    last = max([P.covered_through(), *known])
    days, through = schedule(observed)
    for s in days:
        if last < s and (s < day if before else s <= day):
            return (f"possibly stale: RBI's MPC was scheduled to decide on {s:%d-%b-%Y} and no decision for it has "
                    f"been read from RBI's RSS feed (the typed table was last checked on {P.REVIEWED:%d-%b-%Y})")  # fmt: skip
    if (day - timedelta(days=1) if before else day) > through:
        return (f"possibly stale: no MPC schedule is known after {through:%d-%b-%Y} (the event calendar lists none), "
                "so a newer decision may be missing")  # fmt: skip
    return None

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
from datetime import date, datetime
from decimal import Decimal


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

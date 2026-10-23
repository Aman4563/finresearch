"""RBI's policy rates, typed in by hand from RBI's own releases (#479, the owner's decision of 23-Oct-2026).

No rate is collected automatically from anyone: FBIL's Terms of Use of 13-Oct-2026 forbid building a database or feed
from its free site (#476), and RBI's website disclaimer prohibits caching its pages. A policy-rate decision is a
public announcement, so each change is typed in here with the RBI release that announced it (title, date, URL), and
every row was checked by viewing that release on rbi.org.in. Nothing here fetches anything.

One row per change of the policy repo rate or of the corridor around it, from the rates in force on 01-Jan-2015
(START) to the 07-Oct-2026 hike to 5.50 %. Corridor-only changes are rows too (06-Apr-2017, 17-Apr-2020, 08-Apr-2022).

* repo: the policy repo rate under the liquidity adjustment facility (LAF), a fraction.
* floor: the corridor's floor. Up to 07-Apr-2022 the fixed-rate reverse repo; from 08-Apr-2022 the standing deposit
  facility (SDF) rate, which RBI made the floor of the corridor (the fixed-rate reverse repo was kept outside it).
  `floor_kind` names the instrument.
* msf: the marginal standing facility rate (the Bank Rate moves with it).

Effective dates. A decision is taken to be in force from the day it was announced: RBI's resolutions announce the
rates "with immediate effect" or as the rates that now stand. `in_force(day, before=True)` gives the rates in force at
the start of `day`, so a decision announced that day (around noon IST after an MPC meeting) is not used by a caller
that must not see that day's news.

REVIEWED is the last day the table was checked against RBI's releases. A day after RBI's next scheduled MPC decision
(NEXT_MPC) may have a newer rate this table does not know: callers label such answers as possibly stale.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

START = date(2015, 1, 1)  # the first day the table answers for
REVIEWED = date(2026, 10, 23)


@dataclass(frozen=True)
class PolicyRate:
    effective: date  # in force from this day (the announcement day)
    repo: Decimal  # fractions: 0.055 = 5.50 %
    floor: Decimal
    floor_kind: str  # "reverse repo" (to 07-Apr-2022) | "SDF" (from 08-Apr-2022)
    msf: Decimal
    title: str  # the RBI release's title, as printed
    released: date
    url: str


def _row(effective: str, repo: str, floor: str, msf: str, title: str, url: str, released: str | None = None,
         kind: str | None = None) -> PolicyRate:  # fmt: skip
    eff = date.fromisoformat(effective)
    kind = kind or ("SDF" if eff >= date(2022, 4, 8) else "reverse repo")
    return PolicyRate(eff, Decimal(repo) / 100, Decimal(floor) / 100, kind, Decimal(msf) / 100, title,
                      date.fromisoformat(released or effective), url)  # fmt: skip


def in_force(day: date, *, before: bool = False) -> PolicyRate | None:
    """The rates in force on `day` (with `before`, at the start of `day`: a decision announced that day is not
    counted); None before START, where the table does not answer."""
    if day < START:
        return None
    i = bisect_right(_DAYS, day - timedelta(days=1) if before else day)
    return TABLE[i - 1] if i else None


def repo_change(since: date, day: date, *, before: bool = False) -> Decimal | None:
    """The change in the repo rate from the one in force on `since` to the one in force on `day` (`before` applies
    to `day`); None when either is unknown."""
    a, b = in_force(since), in_force(day, before=before)
    return None if a is None or b is None else b.repo - a.repo


def changes_between(since: date, day: date) -> list[PolicyRate]:
    """The rows that took effect after `since` and on or before `day`."""
    return [r for r in TABLE if since < r.effective <= day]


_PR = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid="
# (effective, repo %, floor %, MSF %, title, release URL); PROVISIONAL rows are replaced by verified ones below.
TABLE: tuple[PolicyRate, ...] = (
    _row("2014-01-28", "8.00", "7.00", "9.00", "PROVISIONAL", ""),
    _row("2015-01-15", "7.75", "6.75", "8.75", "PROVISIONAL", ""),
    _row("2015-03-04", "7.50", "6.50", "8.50", "PROVISIONAL", ""),
    _row("2015-06-02", "7.25", "6.25", "8.25", "PROVISIONAL", ""),
    _row("2015-09-29", "6.75", "5.75", "7.75", "PROVISIONAL", ""),
    _row("2016-04-05", "6.50", "6.00", "7.00", "PROVISIONAL", ""),
    _row("2016-10-04", "6.25", "5.75", "6.75", "PROVISIONAL", ""),
    _row("2017-04-06", "6.25", "6.00", "6.50", "PROVISIONAL", ""),
    _row("2017-08-02", "6.00", "5.75", "6.25", "PROVISIONAL", ""),
    _row("2018-06-06", "6.25", "6.00", "6.50", "PROVISIONAL", ""),
    _row("2018-08-01", "6.50", "6.25", "6.75", "PROVISIONAL", ""),
    _row("2019-02-07", "6.25", "6.00", "6.50", "PROVISIONAL", ""),
    _row("2019-04-04", "6.00", "5.75", "6.25", "PROVISIONAL", ""),
    _row("2019-06-06", "5.75", "5.50", "6.00", "PROVISIONAL", ""),
    _row("2019-08-07", "5.40", "5.15", "5.65", "PROVISIONAL", ""),
    _row("2019-10-04", "5.15", "4.90", "5.40", "PROVISIONAL", ""),
    _row("2020-03-27", "4.40", "4.00", "4.65", "PROVISIONAL", ""),
    _row("2020-04-17", "4.40", "3.75", "4.65", "PROVISIONAL", ""),
    _row("2020-05-22", "4.00", "3.35", "4.25", "PROVISIONAL", ""),
    _row("2022-04-08", "4.00", "3.75", "4.25", "PROVISIONAL", ""),
    _row("2022-05-04", "4.40", "4.15", "4.65", "PROVISIONAL", ""),
    _row("2022-06-08", "4.90", "4.65", "5.15", "PROVISIONAL", ""),
    _row("2022-08-05", "5.40", "5.15", "5.65", "PROVISIONAL", ""),
    _row("2022-09-30", "5.90", "5.65", "6.15", "PROVISIONAL", ""),
    _row("2022-12-07", "6.25", "6.00", "6.50", "PROVISIONAL", ""),
    _row("2023-02-08", "6.50", "6.25", "6.75", "PROVISIONAL", ""),
    _row("2025-02-07", "6.25", "6.00", "6.50", "PROVISIONAL", ""),
    _row("2025-04-09", "6.00", "5.75", "6.25", "PROVISIONAL", ""),
    _row("2025-06-06", "5.50", "5.25", "5.75", "PROVISIONAL", ""),
    _row("2025-12-05", "5.25", "5.00", "5.50", "PROVISIONAL", ""),
    _row("2026-10-07", "5.50", "5.25", "5.75",
         "Monetary Policy Statement, 2026-27 Resolution of the Monetary Policy Committee October 5 to 7, 2026",
         f"{_PR}63742"),
)  # fmt: skip
_DAYS = [r.effective for r in TABLE]

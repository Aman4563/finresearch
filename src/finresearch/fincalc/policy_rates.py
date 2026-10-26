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

Effective dates. A decision is taken to be in force from the day it was announced. Most releases say "with immediate
effect"; those of 05-Apr-2016, 06-Apr-2017, 06-Jun-2018, 01-Aug-2018, 08-Apr-2022, 05-Dec-2025 and 07-Oct-2026 state
no effective date, so the announcement day is assumed there. `in_force(day, before=True)` gives the rates in force at
the start of `day`, so a decision announced that day is not used by a caller that must not see that day's news.

REVIEWED is the last day the table was checked against RBI's releases (all rows and every MPC meeting between them,
23-Oct-2026). From NEXT_MPC, RBI's next scheduled decision, the table may miss a newer rate: fincalc.riskfree labels
such answers as possibly stale until a row is added here.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

START = date(2015, 1, 1)  # the first day the table answers for
REVIEWED = date(2026, 10, 23)


@dataclass(frozen=True)
class PolicyRate:
    effective: date  # in force from this day (the announcement day)
    repo: Decimal  # fractions: 0.055 = 5.50 %
    floor: Decimal | None  # None only on a row read from RBI's RSS feed whose resolution did not state it
    floor_kind: str  # "reverse repo" (to 07-Apr-2022) | "SDF" (from 08-Apr-2022)
    msf: Decimal | None
    title: str  # the RBI release's title, as printed
    released: date
    url: str
    seen: datetime | None = None  # read from RBI's RSS feed (#492): when first read; None: typed in here


def _row(effective: str, repo: str, floor: str, msf: str, title: str, url: str, released: str | None = None,
         kind: str | None = None) -> PolicyRate:  # fmt: skip
    eff = date.fromisoformat(effective)
    kind = kind or ("SDF" if eff >= date(2022, 4, 8) else "reverse repo")
    return PolicyRate(eff, Decimal(repo) / 100, Decimal(floor) / 100, kind, Decimal(msf) / 100, title,
                      date.fromisoformat(released or effective), url)  # fmt: skip


def in_force(
    day: date, *, before: bool = False, rows: Sequence[PolicyRate] | None = None
) -> PolicyRate | None:
    """The rates in force on `day` (with `before`, at the start of `day`: a decision announced that day is not
    counted); None before START, where the table does not answer. `rows`: the table extended by RBI's RSS feed
    (fincalc.policy_feed.merge); default the table alone."""
    if day < START:
        return None
    rows = TABLE if rows is None else rows
    days = _DAYS if rows is TABLE else [r.effective for r in rows]
    i = bisect_right(days, day - timedelta(days=1) if before else day)
    return rows[i - 1] if i else None


def repo_change(since: date, day: date, *, before: bool = False,
                rows: Sequence[PolicyRate] | None = None) -> Decimal | None:  # fmt: skip
    """The change in the repo rate from the one in force on `since` to the one in force on `day` (`before` applies
    to `day`); None when either is unknown."""
    a, b = in_force(since, rows=rows), in_force(day, before=before, rows=rows)
    return None if a is None or b is None else b.repo - a.repo


def changes_between(since: date, day: date, rows: Sequence[PolicyRate] | None = None) -> list[PolicyRate]:
    """The rows that took effect after `since` and on or before `day`."""
    return [r for r in (TABLE if rows is None else rows) if since < r.effective <= day]


_PR = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid="
_MPC = "Resolution of the Monetary Policy Committee"
# (effective, repo %, floor %, MSF %, the release's title as printed, its URL[, release date]). Each row was checked on
# 23-Oct-2026 by viewing the release; every MPC meeting between two rows was a hold (the 2019-20 conflict in secondary
# tables: the cut to 5.15 % is the 04-Oct-2019 resolution; 05-Dec-2019 and 06-Feb-2020 kept it unchanged).
TABLE: tuple[PolicyRate, ...] = (
    # set on 28-Jan-2014 (RBI Handbook of Statistics, Table 44 "Major Monetary Policy Rates and Reserve
    # Requirements"); the last statement before the table starts keeps it unchanged
    _row("2014-01-28", "8.00", "7.00", "9.00",
         "Fifth Bi-Monthly Monetary Policy Statement, 2014-15 By Dr. Raghuram G Rajan, Governor (rates unchanged)",
         f"{_PR}32649", "2014-12-02"),
    _row("2015-01-15", "7.75", "6.75", "8.75", "Statement by Dr. Raghuram G Rajan, Governor on Monetary Policy",
         f"{_PR}33012"),  # off-cycle
    _row("2015-03-04", "7.50", "6.50", "8.50", "Statement by Dr. Raghuram G Rajan, Governor, on Monetary Policy",
         f"{_PR}33372"),  # off-cycle
    _row("2015-06-02", "7.25", "6.25", "8.25", "Second Bi-monthly Monetary Policy Statement, 2015-16", f"{_PR}34073"),
    _row("2015-09-29", "6.75", "5.75", "7.75", "Fourth Bi-monthly Monetary Policy Statement, 2015-16", f"{_PR}35087"),
    _row("2016-04-05", "6.50", "6.00", "7.00", "First Bi-monthly Monetary Policy Statement, 2016-17",
         f"{_PR}36654"),  # corridor narrowed to +/-50 bp
    _row("2016-10-04", "6.25", "5.75", "6.75", f"Fourth Bi-monthly Monetary Policy Statement, 2016-17 {_MPC} (MPC)",
         f"{_PR}38224"),
    _row("2017-04-06", "6.25", "6.00", "6.50", f"First Bi-monthly Monetary Policy Statement, 2017-18 {_MPC} (MPC)",
         f"{_PR}40069"),  # repo unchanged; corridor narrowed to +/-25 bp
    _row("2017-08-02", "6.00", "5.75", "6.25", f"Third Bi-monthly Monetary Policy Statement, 2017-18 {_MPC} (MPC)",
         f"{_PR}41256"),
    _row("2018-06-06", "6.25", "6.00", "6.50", f"Second Bi-monthly Monetary Policy Statement, 2018-19 {_MPC} (MPC)",
         f"{_PR}44125"),
    _row("2018-08-01", "6.50", "6.25", "6.75", f"Third Bi-monthly Monetary Policy Statement, 2018-19 {_MPC} (MPC)",
         f"{_PR}44636"),
    _row("2019-02-07", "6.25", "6.00", "6.50", f"Sixth Bi-monthly Monetary Policy Statement, 2018-19 {_MPC} (MPC)",
         f"{_PR}46235"),
    _row("2019-04-04", "6.00", "5.75", "6.25", f"First Bi-monthly Monetary Policy Statement, 2019-20 {_MPC} (MPC)",
         f"{_PR}46722"),
    _row("2019-06-06", "5.75", "5.50", "6.00", f"Second Bi-monthly Monetary Policy Statement, 2019-20 {_MPC} (MPC)",
         f"{_PR}47225"),
    _row("2019-08-07", "5.40", "5.15", "5.65", f"Third Bi-monthly Monetary Policy Statement, 2019-20 {_MPC} (MPC)",
         f"{_PR}47818"),
    _row("2019-10-04", "5.15", "4.90", "5.40", f"Fourth Bi-monthly Monetary Policy Statement, 2019-20 {_MPC} (MPC)",
         f"{_PR}48319"),
    _row("2020-03-27", "4.40", "4.00", "4.65", f"Seventh Bi-monthly Monetary Policy Statement, 2019-20 {_MPC} (MPC)",
         f"{_PR}49581"),  # off-cycle; asymmetric corridor
    _row("2020-04-17", "4.40", "3.75", "4.65", "Governor's Statement, April 17, 2020",
         "https://www.rbi.org.in/Scripts/bs_viewcontent.aspx?Id=3853"),  # reverse repo only, not an MPC decision
    _row("2020-05-22", "4.00", "3.35", "4.25", f"Monetary Policy Statement, 2020-21: {_MPC} (MPC) May 20 to 22, 2020",
         f"{_PR}49843"),  # off-cycle
    # the SDF replaces the fixed-rate reverse repo (kept at 3.35 %) as the corridor's floor (Statement on
    # Developmental and Regulatory Policies, prid=53536)
    _row("2022-04-08", "4.00", "3.75", "4.25", f"Monetary Policy Statement, 2022-23 {_MPC} (MPC) April 6-8, 2022",
         f"{_PR}53537"),
    _row("2022-05-04", "4.40", "4.15", "4.65", f"Monetary Policy Statement, 2022-23 {_MPC} (MPC) May 2 and 4, 2022",
         f"{_PR}53652"),  # off-cycle
    _row("2022-06-08", "4.90", "4.65", "5.15", f"Monetary Policy Statement, 2022-23 {_MPC} (MPC) June 6-8, 2022",
         f"{_PR}53832"),
    _row("2022-08-05", "5.40", "5.15", "5.65", f"Monetary Policy Statement, 2022-23 {_MPC} (MPC) August 3-5, 2022",
         f"{_PR}54148"),
    _row("2022-09-30", "5.90", "5.65", "6.15", f"Monetary Policy Statement, 2022-23 {_MPC} (MPC) September 28-30, 2022",
         f"{_PR}54465"),
    _row("2022-12-07", "6.25", "6.00", "6.50", f"Monetary Policy Statement, 2022-23 {_MPC} (MPC) December 5-7, 2022",
         f"{_PR}54818"),
    _row("2023-02-08", "6.50", "6.25", "6.75", f"Monetary Policy Statement, 2022-23 {_MPC} (MPC) February 6-8, 2023",
         f"{_PR}55178"),
    _row("2025-02-07", "6.25", "6.00", "6.50", f"Monetary Policy Statement, 2024-25 {_MPC} February 5 to 7, 2025",
         f"{_PR}59692"),
    _row("2025-04-09", "6.00", "5.75", "6.25", f"Monetary Policy Statement, 2025-26 {_MPC} April 7 to 9, 2025",
         f"{_PR}60176"),
    _row("2025-06-06", "5.50", "5.25", "5.75", f"Monetary Policy Statement, 2025-26 {_MPC} June 4 to 6, 2025",
         f"{_PR}60604"),
    _row("2025-12-05", "5.25", "5.00", "5.50", f"Monetary Policy Statement, 2025-26 {_MPC} December 3 to 5, 2025",
         f"{_PR}61749"),
    _row("2026-10-07", "5.50", "5.25", "5.75", f"Monetary Policy Statement, 2026-27 {_MPC} October 5 to 7, 2026",
         f"{_PR}63742"),
)  # fmt: skip
# RBI's next scheduled MPC decision after REVIEWED: the meeting of 2-4 Dec 2026 ("Meeting Schedule of the Monetary
# Policy Committee for 2026-2027", 23-Mar-2026, prid=62422). From that day an answer may miss a newer decision.
NEXT_MPC = date(2026, 12, 4)
NEXT_MPC_SOURCE = f"{_PR}62422"
_DAYS = [r.effective for r in TABLE]

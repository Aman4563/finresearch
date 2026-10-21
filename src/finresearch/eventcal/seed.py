"""The hand-curated event calendar (#367, MF15: index-level events only), each date read off an official page on
22-Oct-2026: RBI MPC decisions (rbi.org.in press releases), Union Budgets (indiabudget.gov.in speech PDFs), the 2024
general-election counting day (ECI press note 23) and FOMC statement days (federalreserve.gov), from 2022.

known_since is the end (23:59 IST) of the day the announcing document was published, so a session replayed on that
day never sees it (US releases: the next IST day). Where no announcing document was found it is the event day itself
(the event is then never visible in advance). A meeting moved by a later notice keeps its old date until that notice
(`withdrawn_since`) and the new date from it. Loaded with `finresearch events seed`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from finresearch.eventcal.calendar import TITLES, Coverage, Event
from finresearch.fincalc.dates import IST

NY = ZoneInfo("America/New_York")
RBI = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid={}"
BUDGET = "https://www.indiabudget.gov.in/doc/bspeech/{}"
PIB = "https://www.pib.gov.in/PressReleasePage.aspx?PRID={}"
ECI_PN23 = "https://elections24.eci.gov.in/docs/press-note-no-23.pdf"  # ECI/PN/23/2024 of 16-Mar-2024
FOMC = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_PR = "https://www.federalreserve.gov/newsevents/pressreleases/{}.htm"


def eod(d: date, days_later: int = 0) -> datetime:
    """The end of IST day `d` (+ days_later): a document published that day is usable from the next session."""
    return datetime.combine(d + timedelta(days=days_later), dtime(23, 59, 59), IST)


D = date.fromisoformat

# RBI MPC: (decision day, source prid, schedule published, its prid, withdrawn by a notice of (day, prid) or None)
_RBI_SCHEDULES = {  # financial year -> (published, prid) of "Schedule of Monetary Policy Committee Meetings"
    2022: (D("2021-03-31"), 51350), 2023: (D("2022-03-30"), 53486), 2024: (D("2023-03-24"), 55410),
    2025: (D("2024-03-27"), 57580), 2026: (D("2025-03-26"), 60062), 2027: (D("2026-03-23"), 62422),
}  # fmt: skip
# (decision day, resolution prid or None for a scheduled one not yet held, FY, moved-by notice (day, prid) or None)
_RBI = [
    ("2022-02-09", None, 2022, ("2022-02-06", 53228)),  # scheduled 7-9 Feb; moved to 8-10 Feb
    ("2022-02-10", 53247, 2022, None), ("2022-04-08", 53537, 2023, None), ("2022-06-08", 53832, 2023, None),
    ("2022-08-04", None, 2023, ("2022-07-21", 54069)),  # scheduled 2-4 Aug; moved to 3-5 Aug
    ("2022-08-05", 54148, 2023, None), ("2022-09-30", 54465, 2023, None), ("2022-12-07", 54818, 2023, None),
    ("2023-02-08", 55178, 2023, None), ("2023-04-06", 55472, 2024, None), ("2023-06-08", 55814, 2024, None),
    ("2023-08-10", 56173, 2024, None), ("2023-10-06", 56502, 2024, None), ("2023-12-08", 56888, 2024, None),
    ("2024-02-08", 57275, 2024, None), ("2024-04-05", 57637, 2025, None), ("2024-06-07", 58050, 2025, None),
    ("2024-08-08", 58447, 2025, None), ("2024-10-09", 58850, 2025, None), ("2024-12-06", 59244, 2025, None),
    ("2025-02-07", 59692, 2025, None), ("2025-04-09", 60176, 2026, None), ("2025-06-06", 60604, 2026, None),
    ("2025-08-07", None, 2026, ("2025-06-06", 60612)),  # scheduled 5-7 Aug; moved to 4-6 Aug
    ("2025-08-06", 60957, 2026, None), ("2025-10-01", 61332, 2026, None), ("2025-12-05", 61749, 2026, None),
    ("2026-02-06", 62169, 2026, None), ("2026-04-08", 62514, 2027, None), ("2026-06-05", 62863, 2027, None),
    ("2026-08-05", 63287, 2027, None), ("2026-10-07", 63742, 2027, None),
    ("2026-12-04", None, 2027, None), ("2027-02-05", None, 2027, None),  # scheduled 2-4 Dec, 3-5 Feb: last day
]  # fmt: skip
# moved meetings: the new date is known from the notice that moved it
_MOVED_TO = {"2022-02-10": ("2022-02-06", 53228), "2022-08-05": ("2022-07-21", 54069),
             "2025-08-06": ("2025-06-06", 60612)}  # fmt: skip
# Unscheduled: the resolution itself is the first public notice (+40 bp to 4.40%)
_RBI_OFF_CYCLE = [("2022-05-04", 53652)]


def _rbi() -> list[Event]:
    out = []
    for day, prid, fy, moved in _RBI:
        pub, sched = _RBI_SCHEDULES[fy]
        known, kurl = eod(pub), RBI.format(sched)
        if day in _MOVED_TO:
            nd, nprid = _MOVED_TO[day]
            known, kurl = eod(D(nd)), RBI.format(nprid)
        title = TITLES["rbi_mpc"] + ("" if prid else " (scheduled; decision on the meeting's last day)")
        out.append(Event(type="rbi_mpc", tier=1, scope="IN", day=D(day), at=None, title=title,
                         source_url=RBI.format(prid or sched), known_since=known, known_since_url=kurl,
                         withdrawn_since=eod(D(moved[0])) if moved else None))  # fmt: skip
    for day, prid in _RBI_OFF_CYCLE:
        out.append(Event(type="rbi_mpc", tier=1, scope="IN", day=D(day), at=None,
                         title=TITLES["rbi_mpc"] + " (unscheduled meeting)", source_url=RBI.format(prid),
                         known_since=eod(D(day)), known_since_url=RBI.format(prid)))  # fmt: skip
    return out


# Union Budget: (day, speech file, earliest official mention found (day, url) or None = the day itself, label)
_BUDGETS = [
    ("2022-02-01", BUDGET.format("bs202223.pdf"), ("2022-01-27", PIB.format(1793018)), "2022-23"),
    ("2023-02-01", BUDGET.format("bs2023_24.pdf"), None, "2023-24"),
    ("2024-02-01", BUDGET.format("bs2024_25(I).pdf"), None, "2024-25 interim"),
    ("2024-07-23", BUDGET.format("bs2024_25.pdf"), ("2024-07-06", "https://www.newsonair.gov.in/budget-session-of-"
                   "parliament-to-begin-from-22nd-of-this-month-union-budget-to-be-presented-the-next-day"), "2024-25"),
    ("2025-02-01", BUDGET.format("bs2025_26.pdf"), ("2025-01-18", "https://newsonair.gov.in/budget-session-of-"
                   "parliament-to-begin-on-january-31-union-budget-on-february-1/"), "2025-26"),
    ("2026-02-01", "https://www.indiabudget.gov.in/doc/budget_speech.pdf", ("2026-01-27", PIB.format(2219158)),
     "2026-27"),
]  # fmt: skip
# After each budget no other Union Budget comes before mid-January (annual, Constitution Art. 112; on 1 Feb since
# 2017, its date announced in late January), except that a general election brings a full budget after the results
# (23-Jul-2024 after the 01-Feb-2024 interim one): the interim budget's coverage stops at the 2024 results day.
_BUDGET_UNTIL = {"2022-02-01": "2023-01-15", "2023-02-01": "2024-01-15", "2024-02-01": "2024-06-04",
                 "2024-07-23": "2025-01-15", "2025-02-01": "2026-01-15", "2026-02-01": "2027-01-15"}  # fmt: skip
BUDGET_NOTE = ("annual Union Budget (Art. 112), on 1 Feb since 2017; covered to mid-January, before the next date is "
               "announced; an election year adds a full budget after the results")  # fmt: skip

# FOMC statement days (the last day of each meeting) and the press release that set each year's tentative schedule.
# 2 p.m. ET is stated in the 2024 and 2025 schedule releases (2025-27 rows); earlier rows keep no time (unverified).
_FOMC = {
    ("2021-06-04", "monetary20210604a"): ["2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27",
                                          "2022-09-21", "2022-11-02", "2022-12-14"],
    ("2022-06-24", "monetary20220624a"): ["2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26",
                                          "2023-09-20", "2023-11-01", "2023-12-13"],
    ("2023-06-23", "monetary20230623a"): ["2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
                                          "2024-09-18", "2024-11-07", "2024-12-18"],
    ("2024-08-09", "monetary20240809a"): ["2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
                                          "2025-09-17", "2025-10-29", "2025-12-10", "2026-01-28", "2026-03-18",
                                          "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28",
                                          "2026-12-09"],
    ("2025-09-05", "monetary20250905a"): ["2027-01-27", "2027-03-17"],
}  # fmt: skip


def _budgets() -> list[Event]:
    out = []
    for day, url, first, label in _BUDGETS:
        known, kurl = (eod(D(first[0])), first[1]) if first else (eod(D(day)), None)
        out.append(Event(type="union_budget", tier=1, scope="IN", day=D(day), at=None,
                         title=f"{TITLES['union_budget']} {label}", source_url=url, known_since=known,
                         known_since_url=kurl))  # fmt: skip
    return out


def _fomc() -> list[Event]:
    out = []
    for (pub, pr), days in _FOMC.items():
        for day in days:
            d = D(day)
            at = datetime.combine(d, dtime(14, 0), NY) if d.year >= 2025 else None
            out.append(Event(type="fomc", tier=2, scope="US", day=d, at=at, title=TITLES["fomc"], source_url=FOMC,
                             known_since=eod(D(pub), 1), known_since_url=FED_PR.format(pr)))  # fmt: skip
    return out


ELECTION = Event(type="general_election", tier=1, scope="IN", day=D("2024-06-04"), at=None,
                 title=TITLES["general_election"] + " 2024", source_url=ECI_PN23, known_since=eod(D("2024-03-16")),
                 known_since_url=ECI_PN23)  # fmt: skip


def _coverage() -> list[Coverage]:
    rbi = [Coverage(type="rbi_mpc", scope="IN", through=date(fy, 3, 31), source_url=RBI.format(prid),
                    known_since=eod(pub), note="scheduled meetings only: RBI can meet off-cycle (4-May-2022)")
           for fy, (pub, prid) in _RBI_SCHEDULES.items()]  # fmt: skip
    budget = []
    for e in _budgets():
        budget.append(Coverage(type="union_budget", scope="IN", through=e.day, source_url=e.source_url,
                               known_since=e.known_since, note="the announced budget day"))  # fmt: skip
        budget.append(Coverage(type="union_budget", scope="IN", through=D(_BUDGET_UNTIL[e.day.isoformat()]),
                               source_url=e.source_url, known_since=eod(e.day), note=BUDGET_NOTE))  # fmt: skip
    election = [
        Coverage(type="general_election", scope="IN", through=ELECTION.day, source_url=ECI_PN23,
                 known_since=ELECTION.known_since, note="the 2024 schedule: counting on 4-Jun-2024"),
        Coverage(type="general_election", scope="IN", through=D("2028-12-31"), source_url=ECI_PN23,
                 known_since=eod(ELECTION.day), note="the 18th Lok Sabha's five-year term (Art. 83(2)) runs to June "
                 "2029: no general election before then unless the house is dissolved early; review by end-2028"),
    ]  # fmt: skip
    return rbi + budget + election


EVENTS: list[Event] = sorted([*_rbi(), *_budgets(), ELECTION, *_fomc()], key=lambda e: (e.day, e.type))
COVERAGE: list[Coverage] = _coverage()

"""RBI's MPC meeting schedule and rescheduling notices for the event calendar's nightly refresh (#367).

Source (official, read 23-Oct-2026): rbi.org.in's press releases. The month listing
(https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx) is an ASP.NET page: a GET returns the form, and a
postback with `hdnYear`/`hdnMonth` returns that month's releases as `href=BS_PressReleaseDisplay.aspx?prid=N` links.
Two titles matter: "Meeting Schedule of the Monetary Policy Committee for 2026-2027" (published each March, e.g. prid
62422 of 23-Mar-2026) and "Meeting of the Monetary Policy Committee (MPC) – Rescheduled" (e.g. prid 60612 of
06-Jun-2025, 54069 of 21-Jul-2022, 53228 of 06-Feb-2022). A schedule lists each meeting as "April 6, 7 and 8, 2026";
a notice names the old meeting first and the new one last ("scheduled during August 5 – 7, 2025 ... rescheduled to
August 4 – 6, 2025"), after citing the schedule's release ("dated March 26, 2025"), which is skipped. The decision day
is the meeting's last day (the seed's convention, eventcal.seed). Anything that does not parse completely is an
RbiError: nothing is stored from it, so coverage never grows from a half-read page.

RBI's disclaimer (https://www.rbi.org.in/Scripts/Disclaimer.aspx, read 23-Oct-2026): contents "as is" without
warranties; RBI may "deny or restrict access"; no licence or reuse terms. Collection waits for the owner's decision
(data_rights.PENDING["rbi_mpc_schedule"]).
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass
from datetime import date

PAGE = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"
RELEASE = PAGE + "?prid={}"  # the seed's URL form, so a release already stored is recognised
MIN_MEETINGS = 4  # the Act asks for at least four meetings a year (RBI Act s. 45ZI(1)); six since 2016


class RbiError(RuntimeError):
    pass


MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")  # fmt: skip
# "April 6, 7 and 8, 2026" / "August 4 – 6, 2025" / "February 7-9, 2022" / "June 2, 2027": the last day number
_DATE = re.compile(r"(?P<dated>dated\s+)?\b(?P<m>" + "|".join(MONTHS) + r")\s+(?P<days>\d{1,2}(?:\s*(?:,|and|-|–|—|to)"
                   r"\s*\d{1,2})*)\s*,\s*(?P<y>\d{4})\b")  # fmt: skip
_FY = re.compile(r"for\s+(\d{4})\s*-\s*(\d{2,4})")


def _text(page: str) -> str:
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


def parse_listing(page: str) -> list[tuple[int, str]]:
    """(prid, title) of every press release in a month listing, in page order."""
    rows = re.findall(r"href=['\"]?(?:[^'\" >]*/)?BS_PressReleaseDisplay\.aspx\?prid=(\d+)['\"]?[^>]*>(.*?)</a>",
                      page, flags=re.S | re.I)  # fmt: skip
    return [(int(p), _text(t)) for p, t in rows]


def notice_kind(title: str) -> str | None:
    """'schedule', 'reschedule' or None for a release title. An unscheduled meeting's announcement is not read: it
    has no fixed form (the 4-May-2022 meeting was announced by its own resolution), and the schedule's coverage note
    says off-cycle meetings are not covered."""
    t = title.lower()
    if "monetary policy committee" not in t or "minutes" in t or "unscheduled" in t:
        return None
    if "resched" in t:
        return "reschedule"
    return "schedule" if "schedule" in t else None


def listing_form(page: str, *, year: int, month: int) -> dict[str, str]:
    """The postback fields that ask the listing for `month` of `year` (the page's own GetYearMonth script)."""
    fields = {}
    for tag in re.findall(r"<input\b[^>]*>", page, flags=re.I):
        name = re.search(r'name="([^"]+)"', tag)
        if name and (name.group(1).startswith("__") or name.group(1).endswith("$btn")):
            value = re.search(r'value="([^"]*)"', tag)
            fields[name.group(1)] = htmllib.unescape(value.group(1)) if value else ""
    if not fields.get("__VIEWSTATE"):
        raise RbiError("RBI press-release listing answered without its form (no __VIEWSTATE)")
    fields.setdefault("UsrFontCntr$btn", "")
    return fields | {"hdnYear": str(year), "hdnMonth": str(month)}


def _last_days(text: str) -> list[date]:
    out = []
    for m in _DATE.finditer(text):
        if m.group("dated"):
            continue  # the cited release's own date, not a meeting
        last = int(re.findall(r"\d{1,2}", m.group("days"))[-1])
        try:
            out.append(date(int(m.group("y")), MONTHS.index(m.group("m")) + 1, last))
        except ValueError as e:
            raise RbiError(f"RBI notice with an impossible date: {m.group(0)!r}") from e
    return out


@dataclass(frozen=True)
class Schedule:
    fy: tuple[int, int]  # (start year, end year): April to March
    days: list[date]  # decision days, the last day of each meeting

    @property
    def through(self) -> date:
        return date(self.fy[1], 3, 31)


@dataclass(frozen=True)
class Reschedule:
    old: date
    new: date


def _body(page: str) -> str:
    text = _text(page)
    start = text.find("Monetary Policy Committee")
    end = text.rfind("Press Release:")  # the signature line; an earlier one is the cited schedule's number
    return text[max(start, 0) : end if end > start else len(text)]


def parse_schedule(page: str) -> Schedule:
    body = _body(page)
    fy = _FY.search(body)
    if not fy:
        raise RbiError("RBI MPC schedule without its financial year")
    start = int(fy.group(1))
    days = _last_days(body[fy.end() :])
    lo, hi = date(start, 4, 1), date(start + 1, 3, 31)
    if len(days) < MIN_MEETINGS or len(set(days)) != len(days) or any(not lo <= d <= hi for d in days):
        raise RbiError(f"RBI MPC schedule for {start}-{start + 1} did not parse completely: {days}")
    return Schedule(fy=(start, start + 1), days=sorted(days))


def parse_reschedule(page: str) -> Reschedule:
    days = _last_days(_body(page))
    if len(days) < 2 or days[0] == days[-1]:
        raise RbiError(f"RBI MPC rescheduling notice without an old and a new date: {days}")
    return Reschedule(old=days[0], new=days[-1])


# ---- the nightly read (monitor.eventcal through eventcal.refresh)

TITLE_SCHEDULED = "RBI monetary policy decision (scheduled; decision on the meeting's last day)"
TITLE_MOVED = "RBI monetary policy decision (rescheduled; decision on the meeting's last day)"
COVERAGE_NOTE = "scheduled meetings only: RBI can meet off-cycle (4-May-2022)"


def months_to_read(today: date, covered: date | None) -> list[tuple[int, int]]:
    """This month and the last (about 3 requests a night); while the current financial year's schedule is not
    covered, also February and March of its first calendar year, when RBI publishes it (a catch-up after downtime)."""
    prev = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    out = {(today.year, today.month), prev}
    fy_start = today.year if today.month >= 4 else today.year - 1
    if covered is None or covered < date(fy_start + 1, 3, 31):
        out |= {(fy_start, 2), (fy_start, 3)}
    return sorted(out)


async def read(known, now, *, http=None):
    """One complete read: the month listings, then each MPC notice not yet stored. Any refusal or unparsable page
    raises (nothing is stored); rows dated before today are left out (they could never be known in advance)."""
    from finresearch.adapters.http import PoliteClient
    from finresearch.eventcal.calendar import Coverage, Event
    from finresearch.eventcal.refresh import Found, Move
    from finresearch.fincalc.dates import to_ist

    today = to_ist(now).date()
    own = http is None
    http = http or PoliteClient(cache_dir=None)
    found = Found("rbi_mpc_schedule")
    try:
        first = await http.get(PAGE)
        if not first.ok:
            raise RbiError(f"RBI press-release listing HTTP {first.status}")
        notices: dict[int, str] = {}
        for y, m in months_to_read(today, known.covered.get("rbi_mpc")):
            got = await http.post(PAGE, data=listing_form(first.text, year=y, month=m))
            rows = parse_listing(got.text) if got.ok else []
            if not rows:
                raise RbiError(f"RBI press-release listing for {y}-{m:02d}: HTTP {got.status}, no releases")
            found.urls.append(f"{PAGE}#{y}-{m:02d}")
            notices |= {p: k for p, t in rows if (k := notice_kind(t))}
        for prid, kind in sorted(notices.items()):
            url = RELEASE.format(prid)
            if url in known.urls:
                continue
            page = await http.get(url)
            if not page.ok:
                raise RbiError(f"RBI press release {prid}: HTTP {page.status}")
            found.urls.append(url)
            ev = lambda d, title, url=url: Event(type="rbi_mpc", tier=1, scope="IN", day=d, at=None, title=title,  # noqa: E731
                                        source_url=url, known_since=now, known_since_url=url)  # fmt: skip
            if kind == "schedule":
                sched = parse_schedule(page.text)
                found.events += [ev(d, TITLE_SCHEDULED) for d in sched.days if d >= today]
                found.coverage.append(Coverage(type="rbi_mpc", scope="IN", through=sched.through, source_url=url,
                                               known_since=now, note=COVERAGE_NOTE))  # fmt: skip
            else:
                move = parse_reschedule(page.text)
                if move.new >= today:
                    found.events.append(ev(move.new, TITLE_MOVED))
                    found.moves.append(Move("rbi_mpc", "IN", move.old, move.new))
    finally:
        if own:
            await http.aclose()
    return found

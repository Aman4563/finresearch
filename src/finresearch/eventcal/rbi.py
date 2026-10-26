"""RBI's MPC schedule, rescheduling notices and resolutions for the event calendar's nightly refresh (#367), read from
RBI's press-release RSS feed only (#481).

Source (official, read 23-Oct-2026): https://rbi.org.in/pressreleases_rss.xml, listed on RBI's RSS page
(https://www.rbi.org.in/Scripts/rss.aspx), whose feeds "help users to receive automatic site updates". It lists only
the newest ten press releases, each an <item> with a CDATA title, the release's HTML as its description (empty for a
few minutes after posting), a link `https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=N` (prid numbers
are sequential) and a pubDate in IST without a zone ("Fri, 23 Oct 2026 17:30:00"). Three titles matter:
* "Meeting Schedule of the Monetary Policy Committee for 2026-2027" (each March: prid 62422, PR 2025-2026/2306 of
  23-Mar-2026): each meeting as "April 6, 7 and 8, 2026"; the decision day is the meeting's last day (the seed's
  convention); coverage to 31 March.
* "Meeting of the Monetary Policy Committee (MPC) – Rescheduled" (e.g. prid 60612): the old meeting first and the
  new one last, after the cited schedule's date ("dated March 26, 2025"), which is skipped.
* "Monetary Policy Statement, 2026-27 Resolution of the Monetary Policy Committee October 5 to 7, 2026" (prid 63742,
  viewed 23-Oct-2026): the meeting held, and "The next meeting of the MPC is scheduled for December 2 to 4, 2026".
  RBI states the next meeting with no other in between, so a stale date in between is withdrawn: a rescheduling
  notice missed earlier is caught here. A statement never overrides a newer release already stored.
An "Unscheduled meeting" title becomes a notice: such meetings have no fixed form, and coverage is scheduled
meetings only.

Terms (data_rights "rbi_rss", the owner's decision of 23-Oct-2026, #481): RBI provides the feed for syndication; the
site says "All Rights Reserved" and its disclaimer prohibits caching site content. The feed is fetched once a night
without a cache, its text is read in memory, and only facts and links are kept: the dates, the type, the item's link
(in the seed's citation form) and a fixed title. No RBI web page is ever requested.

Completeness: one fetch a night sees only the newest ten releases, so on a busy day the morning's have left the feed
(23-Oct-2026: ten releases between 09:00 and 17:30). The newest prid read is kept in state_dir (eventcal/rbi_rss.json:
metadata about the read, not RBI content); releases published since then that are no longer listed are reported as
a notice, because an MPC item among them was not seen. Coverage is not cut for a gap (every busy day would read
"unknown"); the cadence is the owner's decision (#481). An MPC item whose text is not in the feed yet is a notice and
is read on a later night if still listed. A schedule or rescheduling text that does not parse completely is an
RbiError: nothing is stored from that read.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

FEED = "https://rbi.org.in/pressreleases_rss.xml"
# the citation form of an item's link (the seed's), so a release already stored is recognised; never requested
RELEASE = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid={}"
DATASET = "rbi_rss"  # data_rights
MIN_MEETINGS = 4  # the Act asks for at least four meetings a year (RBI Act s. 45ZI(1)); six since 2016


class RbiError(RuntimeError):
    pass


MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")  # fmt: skip
# "April 6, 7 and 8, 2026" / "August 4 – 6, 2025" / "February 7-9, 2022" / "June 2, 2027": the last day number
_DATE = re.compile(r"(?P<dated>dated\s+)?\b(?P<m>" + "|".join(MONTHS) + r")\s+(?P<days>\d{1,2}(?:\s*(?:,|and|-|–|—|to)"
                   r"\s*\d{1,2})*)\s*,\s*(?P<y>\d{4})\b")  # fmt: skip
_FY = re.compile(r"for\s+(\d{4})\s*-\s*(\d{2,4})")
_NEXT = re.compile(r"next\s+meeting\s+of\s+the\s+(?:MPC|Monetary\s+Policy\s+Committee)(?:\s*\(MPC\))?\s+is\s+"
                   r"scheduled\s+(?:for|during|on)\s+(.{0,60}?\d{4})", re.I)  # fmt: skip
_LINK = re.compile(r"https?://(?:www\.)?rbi\.org\.in/scripts/BS_PressReleaseDisplay\.aspx\?(?:[^#]*&)?prid=(\d+)"
                   r"(?:&[^#]*)?", re.I)  # fmt: skip


def _text(page: str) -> str:
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


@dataclass(frozen=True)
class Item:
    prid: int
    title: str
    published: datetime | None  # aware IST: the feed's pubDate carries no zone
    body: str  # the description's HTML, read in memory only: never stored

    @property
    def url(self) -> str:
        return RELEASE.format(self.prid)

    @property
    def text(self) -> str:
        """The title and the release's text as one plain string."""
        return _text(f"{self.title}. {self.body}")


def _published(raw: str | None) -> datetime | None:
    from finresearch.fincalc.dates import IST

    try:
        dt = parsedate_to_datetime((raw or "").strip())
    except (TypeError, ValueError, IndexError):
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _refuse_dtd(content: bytes) -> None:
    """A first pass with expat whose DOCTYPE and entity handlers raise (defusedxml's approach, no dependency): a feed
    never needs a DTD, and without one no entity can expand (billion laughs) or point outside (XXE)."""
    import xml.parsers.expat

    def refuse(*_a: object) -> None:
        raise RbiError("RBI RSS feed declares a DOCTYPE or entities: refused")

    p = xml.parsers.expat.ParserCreate()
    p.StartDoctypeDeclHandler = refuse
    p.EntityDeclHandler = refuse
    try:
        p.Parse(content, True)
    except xml.parsers.expat.ExpatError as e:
        raise RbiError(f"RBI RSS feed is not well-formed XML: {e}") from e


def parse_feed(content: bytes) -> list[Item]:
    """The feed's press releases in feed order (newest first). Anything but an RSS channel with releases, or a
    document declaring a DOCTYPE or entities (refused before the tree is built), is an RbiError."""
    _refuse_dtd(content)
    root = ET.fromstring(content)  # well-formed and DTD-free (checked above)
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise RbiError(f"RBI RSS feed answered with <{root.tag}>, not an RSS channel")
    items = []
    for it in channel.findall("item"):
        title = _text(it.findtext("title") or "")
        link = _LINK.fullmatch((it.findtext("link") or "").strip())
        if link is None:
            if notice_kind(title):
                raise RbiError(f"RBI MPC release without a press-release link: {title!r}")
            continue  # not a press release this calendar reads
        items.append(Item(prid=int(link.group(1)), title=title, published=_published(it.findtext("pubDate")),
                          body=it.findtext("description") or ""))  # fmt: skip
    if not items:
        raise RbiError("RBI RSS feed lists no press releases")
    return items


def notice_kind(title: str) -> str | None:
    """'schedule', 'reschedule', 'resolution' or 'unscheduled' for an MPC release title; None otherwise (minutes)."""
    t = title.lower()
    if "monetary policy committee" not in t or "minutes" in t:
        return None
    if "resolution" in t:
        return "resolution"
    if "unscheduled" in t:
        return "unscheduled"
    if "resched" in t:
        return "reschedule"
    return "schedule" if "schedule" in t else None


def _last_days(text: str) -> list[date]:
    out = []
    for m in _DATE.finditer(text):
        if m.group("dated"):
            continue  # the cited release's own date, not a meeting
        last = int(re.findall(r"\d{1,2}", m.group("days"))[-1])
        try:
            out.append(date(int(m.group("y")), MONTHS.index(m.group("m")) + 1, last))
        except ValueError as e:
            raise RbiError(f"RBI MPC release with an impossible date: {m.group(0)!r}") from e
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


@dataclass(frozen=True)
class Resolution:
    held: date  # the decision day: the meeting's last day, from the title
    next: date | None  # the next meeting's last day as the resolution states it; None when it states none


def _body(text: str) -> str:
    text = _text(text)
    start = text.find("Monetary Policy Committee")
    end = text.rfind("Press Release:")  # the signature line; an earlier one is the cited schedule's number
    return text[max(start, 0) : end if end > start else len(text)]


def parse_schedule(text: str) -> Schedule:
    """An MPC schedule from its title and text (Item.text)."""
    body = _body(text)
    fy = _FY.search(body)
    if not fy:
        raise RbiError("RBI MPC schedule without its financial year")
    start = int(fy.group(1))
    days = _last_days(body[fy.end() :])
    lo, hi = date(start, 4, 1), date(start + 1, 3, 31)
    if len(days) < MIN_MEETINGS or len(set(days)) != len(days) or any(not lo <= d <= hi for d in days):
        raise RbiError(f"RBI MPC schedule for {start}-{start + 1} did not parse completely: {days}")
    return Schedule(fy=(start, start + 1), days=sorted(days))


def parse_reschedule(text: str) -> Reschedule:
    """A rescheduling notice from its title and text (Item.text): the first meeting named is the old one, the last
    the new one."""
    days = _last_days(_body(text))
    if len(days) < 2 or days[0] == days[-1]:
        raise RbiError(f"RBI MPC rescheduling notice without an old and a new date: {days}")
    return Reschedule(old=days[0], new=days[-1])


def parse_resolution(item: Item) -> Resolution:
    """The meeting held (the last date in the title) and the next meeting the text names, when it names a later one."""
    days = _last_days(item.title)
    if not days:
        raise RbiError(f"RBI MPC resolution without its meeting dates in the title: {item.title!r}")
    m = _NEXT.search(_text(item.body))
    later = _last_days(m.group(1)) if m else []
    nxt = later[-1] if later and later[-1] > days[-1] else None
    return Resolution(held=days[-1], next=nxt)


# ---- the nightly read (monitor.eventcal through eventcal.refresh)

TITLE_SCHEDULED = "RBI monetary policy decision (scheduled; decision on the meeting's last day)"
TITLE_MOVED = "RBI monetary policy decision (rescheduled; decision on the meeting's last day)"
COVERAGE_NOTE = "scheduled meetings only: RBI can meet off-cycle (4-May-2022)"


def state_file() -> Path:
    """Where the newest prid read is kept (metadata about the read, not RBI content)."""
    from finresearch.config import get_settings

    return Path(get_settings().state_dir) / "eventcal" / "rbi_rss.json"


def _last_read(path: Path) -> dict | None:
    try:
        got = json.loads(path.read_text())
        return got if isinstance(got, dict) and isinstance(got.get("newest_prid"), int) else None
    except (OSError, ValueError):
        return None


def _when(d: datetime | str | None) -> str:
    if isinstance(d, str):
        try:
            d = datetime.fromisoformat(d)
        except ValueError:
            return d
    return d.strftime("%Y-%m-%d %H:%M") if d else "an unknown time"


def gap_notice(last: dict | None, items: list[Item]) -> str | None:
    """The releases published since the last read that are no longer in the feed (sequential prids), if any."""
    if not last:
        return None
    oldest = min(items, key=lambda i: i.prid)
    missed = oldest.prid - last["newest_prid"] - 1
    if missed <= 0:
        return None
    lo, hi = last["newest_prid"] + 1, oldest.prid - 1
    return (f"RBI press-release feed: {missed} release(s) (prid {lo}-{hi}) published between "
            f"{_when(last.get('newest_published'))} and {_when(oldest.published)} IST were no longer listed (the feed "
            f"keeps only its newest {len(items)}); an MPC schedule, rescheduling notice or resolution among them was "
            "not seen (#481)")  # fmt: skip


def _record(path: Path, last: dict | None, items: list[Item], now: datetime) -> None:
    newest = max(items, key=lambda i: i.prid)
    keep = {
        "newest_prid": newest.prid,
        "newest_published": newest.published.isoformat() if newest.published else None,
    }
    if last and last["newest_prid"] > newest.prid:  # never moves back
        keep = {"newest_prid": last["newest_prid"], "newest_published": last.get("newest_published")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep | {"read_at": now.isoformat()}))
    tmp.replace(path)


async def read(known, now, *, http=None, state_path: Path | None = None):
    """One GET of the feed: the MPC facts in it not yet stored, oldest release first, and a notice for releases that
    left the feed since the last read. A refusal or an unparsable schedule or rescheduling text raises (nothing is
    stored, the last-read marker stays); meetings dated before today are left out (never known in advance)."""
    from finresearch.adapters.http import PoliteClient
    from finresearch.eventcal.calendar import TITLES, Coverage, Event
    from finresearch.eventcal.refresh import Found, Move, Replace
    from finresearch.fincalc.dates import to_ist

    today = to_ist(now).date()
    own = http is None
    http = http or PoliteClient(
        cache_dir=None
    )  # never cached: RBI's disclaimer prohibits caching its content
    try:
        got = await http.get(FEED)
    finally:
        if own:
            await http.aclose()
    if not got.ok:
        raise RbiError(f"RBI RSS feed HTTP {got.status}")
    items = parse_feed(got.content)
    path = state_path or state_file()
    last = _last_read(path)
    found = Found(DATASET, urls=[FEED])
    if gap := gap_notice(last, items):
        found.notices.append(gap)
    stored = [int(m.group(1)) for u in known.urls if (m := _LINK.fullmatch(u))]
    newest_stored = max(stored, default=0)
    for item in sorted(items, key=lambda i: i.prid):
        kind = notice_kind(item.title)
        if kind is None or item.url in known.urls:
            continue
        if kind == "unscheduled":
            found.notices.append(f"RBI announced an MPC meeting outside the schedule; not read into the calendar (no "
                                 f"fixed form): {item.title} ({item.url})")  # fmt: skip
            continue
        if not _text(item.body):
            found.notices.append(f"RBI MPC {kind} (tier 1) published {_when(item.published)} IST has no text in the "
                                 f"feed yet, so its dates were not read: {item.title} ({item.url})")  # fmt: skip
            continue

        def ev(d: date, title: str, url: str = item.url) -> Event:
            return Event(type="rbi_mpc", tier=1, scope="IN", day=d, at=None, title=title, source_url=url,
                         known_since=now, known_since_url=url)  # fmt: skip

        if kind == "schedule":
            sched = parse_schedule(item.text)
            found.events += [ev(d, TITLE_SCHEDULED) for d in sched.days if d >= today]
            found.coverage.append(Coverage(type="rbi_mpc", scope="IN", through=sched.through, source_url=item.url,
                                           known_since=now, note=COVERAGE_NOTE))  # fmt: skip
        elif kind == "reschedule":
            move = parse_reschedule(item.text)
            if move.new >= today:
                found.events.append(ev(move.new, TITLE_MOVED))
                found.moves.append(Move("rbi_mpc", "IN", move.old, move.new))
        else:
            try:
                res = parse_resolution(item)
            except RbiError as e:
                found.notices.append(f"{e} ({item.url})")
                continue
            found.events.append(ev(res.held, TITLES["rbi_mpc"]))
            if res.next is None:
                found.notices.append(f"RBI MPC resolution names no later meeting: {item.title} ({item.url})")
            elif (
                res.next >= today and item.prid > newest_stored
            ):  # an older statement never undoes a newer notice
                found.events.append(ev(res.next, TITLE_SCHEDULED))
                found.replaces.append(Replace("rbi_mpc", "IN", res.held, res.next))
    _record(path, last, items, now)
    return found

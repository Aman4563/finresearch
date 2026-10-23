"""India CPI and GDP release dates from MoSPI's Advance Release Calendar, for the event calendar's nightly refresh (#367).
Context events (tier 2, T17): shown beside the tier-1 check, never tier 1.

Source (official, read 23-Oct-2026): www.mospi.gov.in is a JavaScript app; its release-calendar page links "Advance
Release Calendar 2026-27" through `GET https://www.mospi.gov.in/api/documents/get-latest-release-calender`, which
answers `{"status": "success", "data": {"url": "uploads/documents/releaseCalender/<ts>-ARC_2026_27_Sept_2026.pdf"}}`.
The PDF (8 pages) is a table: S.no and month, "Date of release*" ("12th Nov", "12th" over "March", "7thJan"), the
release title, and a link for releases already out. MoSPI revises it during the year (the file name carries a
timestamp), so a new URL is a new version, and its list replaces the stored future dates (refresh.Replace). Only the
"All India Consumer Price Index" and GDP-estimate rows are kept. The calendar states no release time and says "In
case of holidays, reports release on next working day" and "Dates indicated may change in case of exigencies": rows
carry the planned date with no time, and that caveat goes in the title and coverage note. MoSPI's web release list
(`/api/release-calender/fetch-all-release-calender-Web`) holds only past releases and is not used.

The release calendar is a different dataset from the CPI series (adapters.mospi_cpi, api.mospi.gov.in): it has its
own data-rights entry (data_rights.PENDING["mospi_release_calendar"]). No licence text could be read
(mospi.gov.in's policy pages render only with JavaScript). Two requests a night at most (the pointer, then the PDF
only when its URL is new).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

BASE = "https://www.mospi.gov.in/"
LATEST = BASE + "api/documents/get-latest-release-calender"
CPI_TITLE = "India CPI release (MoSPI planned date; next working day if a holiday)"
GDP_TITLE = "India GDP estimates release (MoSPI planned date; next working day if a holiday)"
NOTE = ("MoSPI Advance Release Calendar {fy}: planned dates; a release falling on a holiday moves to the next working "
        "day, and dates may change in case of exigencies")  # fmt: skip
MIN_GDP = 4  # four quarterly estimates a year, plus the advance estimates


class MospiCalendarError(RuntimeError):
    pass


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sept", "Sep", "Oct", "Nov", "Dec")  # fmt: skip
_MONTH_WORD = re.compile(r"\b(" + "|".join(_NAMES) + r")\b")
_ORDINAL = re.compile(r"(?<![\d/])(\d{1,2})(?:st|nd|rd|th)(?=\s|[A-Z]|$)")
_FY = re.compile(r"ADVANCE RELEASE CALENDAR\s*\((\d{4})\s*-\s*(\d{2})\)", re.I)
_DATE_COL = 30  # the date column ends well before the title column; later ordinals are "Released on 13th ..."
_URLISH = re.compile(r"\S*(?:://|\.aspx|\.pdf|PRID=|_)\S*")
_CPI = re.compile(r"Consumer\s+Price\s+Index", re.I)
_GDP = re.compile(r"\bGDP\b|Gross\s+Domestic\s+Product", re.I)


@dataclass(frozen=True)
class Arc:
    fy: tuple[int, int]
    days: dict[str, list[date]]  # india_cpi / india_gdp -> planned release days

    @property
    def through(self) -> date:
        return date(self.fy[1], 3, 31)


def _month(word: str) -> int:
    return _MONTHS.index(word[:3].lower()) + 1


def _items(lines: list[str]) -> list[tuple[int, str]]:
    """(month, item text) per dated row. The month is the one written after the day, else the one under it on the
    next line, else the section's month (the S.no/month column)."""
    out: list[tuple[int, str]] = []
    section: int | None = None
    for i, line in enumerate(lines):
        tok = _ORDINAL.search(line)
        if tok is None or tok.start() >= _DATE_COL:
            if out and line.strip():
                out[-1] = (out[-1][0], out[-1][1] + " " + line.strip())
            continue
        before = [m for m in _MONTH_WORD.finditer(line[: tok.start()])]
        if before:
            section = _month(before[-1].group(1))
        rest = line[tok.end() :]
        after = re.match(r"\s*(" + _MONTH_WORD.pattern + r")", rest)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        below = next((m for m in _MONTH_WORD.finditer(nxt) if abs(m.start() - tok.start()) <= 3), None)
        if after:
            month, rest = _month(after.group(1)), rest[after.end() :]
        elif below:
            month = _month(below.group(1))
        elif section is not None:
            month = section
        else:
            raise MospiCalendarError(f"MoSPI release calendar: no month for {line.strip()!r}")
        out.append((month, f"{int(tok.group(1))}|{rest.strip()}"))
    return out


def parse_arc(text: str) -> Arc:
    """The CPI and GDP release days of one Advance Release Calendar; refused unless every month has its CPI date."""
    fy = _FY.search(text)
    if not fy:
        raise MospiCalendarError(
            "MoSPI release calendar without its year (ADVANCE RELEASE CALENDAR (YYYY-YY))"
        )
    start = int(fy.group(1))
    lines = [ln for page in text.split("\f") for ln in page.split("\n")]
    days: dict[str, set[date]] = {"india_cpi": set(), "india_gdp": set()}
    for month, item in _items(lines):
        day, body = item.split("|", 1)
        body = _URLISH.sub(" ", body)
        try:
            d = date(start if month >= 4 else start + 1, month, int(day))
        except ValueError as e:
            raise MospiCalendarError(f"MoSPI release calendar: impossible date {day} of month {month}") from e
        if _CPI.search(body):
            days["india_cpi"].add(d)
        elif _GDP.search(body) and "estimate" in body.lower():
            days["india_gdp"].add(d)
    cpi_months = sorted({d.month for d in days["india_cpi"]})
    if len(days["india_cpi"]) != 12 or cpi_months != list(range(1, 13)) or len(days["india_gdp"]) < MIN_GDP:
        raise MospiCalendarError(f"MoSPI release calendar {start}-{start + 1} did not parse completely: "
                                 f"{len(days['india_cpi'])} CPI dates, {len(days['india_gdp'])} GDP dates")  # fmt: skip
    return Arc(fy=(start, start + 1), days={k: sorted(v) for k, v in days.items()})


def pdf_text(content: bytes) -> str:
    """pdftotext -layout of the calendar PDF (pages joined by \\f), through ingest's extractor."""
    import tempfile
    from pathlib import Path

    from finresearch.ingest.documents import extract_layout_pages

    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "arc.pdf"
        pdf.write_bytes(content)
        return "\f".join(extract_layout_pages(pdf))


async def read(known, now, *, http=None, to_text=pdf_text):
    """The latest calendar's future CPI and GDP days, its coverage to the year end, and a replacement of the stored
    future days (the dates it no longer lists were moved). An already stored calendar URL is not downloaded again."""
    from datetime import timedelta

    from finresearch.adapters.http import PoliteClient
    from finresearch.eventcal.calendar import Coverage, Event
    from finresearch.eventcal.refresh import Found, Replace
    from finresearch.fincalc.dates import to_ist

    today = to_ist(now).date()
    own = http is None
    http = http or PoliteClient(cache_dir=None)
    found = Found("mospi_release_calendar")
    try:
        ptr = await http.get(LATEST)
        try:
            body = ptr.json() if ptr.ok else None
            path = body["data"]["url"] if body and body.get("status") == "success" else None
        except (ValueError, KeyError, TypeError):
            path = None
        if not isinstance(path, str) or not path.lower().endswith(".pdf") or "://" in path:
            raise MospiCalendarError(f"MoSPI release-calendar pointer: HTTP {ptr.status}, no calendar PDF")
        url = BASE + path.lstrip("/")
        found.urls.append(LATEST)
        if url in known.urls:
            return found
        pdf = await http.get(url)
        if not pdf.ok or not pdf.content.startswith(b"%PDF"):
            raise MospiCalendarError(f"MoSPI release calendar {url}: HTTP {pdf.status}, not a PDF")
        found.urls.append(url)
        arc = parse_arc(to_text(pdf.content))
    finally:
        if own:
            await http.aclose()
    note = NOTE.format(fy=f"{arc.fy[0]}-{arc.fy[1] % 100:02d}")
    for kind, title in (("india_cpi", CPI_TITLE), ("india_gdp", GDP_TITLE)):
        found.events += [Event(type=kind, tier=2, scope="IN", day=d, at=None, title=title, source_url=url,
                               known_since=now, known_since_url=url) for d in arc.days[kind] if d >= today]  # fmt: skip
        found.coverage.append(Coverage(type=kind, scope="IN", through=arc.through, source_url=url, known_since=now,
                                       note=note))  # fmt: skip
        # its own year only: next year's calendar read in March must not withdraw this year's remaining dates
        after = max(today - timedelta(days=1), date(arc.fy[0], 3, 31))
        found.replaces.append(Replace(kind, "IN", after, arc.through))
    return found

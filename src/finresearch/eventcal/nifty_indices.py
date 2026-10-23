"""NSE index rebalances (constituent changes) from NSE Indices' press releases, for the event calendar's nightly refresh
(#367). Context events (tier 2): an index-tracking fund trades the change at the close of the session before it takes
effect, so a plan whose holding window spans that session sees it listed.

Source (official, read 23-Oct-2026): https://www.niftyindices.com/press-release, one static page (~660 KB) listing every
NSE Indices press release since 1998 as `<div class="pressItem" data-date="Oct 22, 2026">` blocks, each with a link to
the release PDF and a title carrying the effective date: "Replacements in indices w.e.f. September 30, 2026" (published
15-Sep-2026), "Replacement in Nifty India FPI 150 index w.e.f. September 11, 2026" (04-Sep-2026). Only the titles are
read; no PDF is fetched. NSE's own F&O circular on the same change ("Change in the Constituents of Indices") is dated
the day of that close (NSE/FAOP/76610 of 29-Sep-2026, w.e.f. 30-Sep), so it gives no advance notice and is not used.

* Candidates: titles about a replacement, inclusion, exclusion or change, except index families no F&O plan reads
  (fixed income, SDL, G-Sec, T-Bill, CD/CP, SME Emerge, IPO). Only releases published in the last LOOKBACK_DAYS are
  inspected (the page goes back to 1998). A candidate without a w.e.f. date in its title (3-5 a year on the real page,
  e.g. "Replacements in indices" of 25-Sep-2024) becomes a notice to read the release: no row, no guessed date.
* Day: the trading session before the w.e.f. date (NSE's circulars say "w.e.f. September 30, 2026 (close of September
  29, 2026)"), skipping NSE trading holidays when that year's list is known; otherwise the previous weekday, and the
  title says it is approximate. Two releases on one day collapse into one row (unique type, scope, day).
* No coverage and no withdrawals: the calendar never claims "no rebalance", and a moved effective date leaves the old
  row (an extra context event, never a missing one).

Terms (https://www.niftyindices.com/terms-of-use, read 23-Oct-2026): "You may not conduct any systematic or automated
data collection activities (including scraping, data mining, data extraction and data harvesting) on or in relation to
our website without our express written consent"; the site "may be used only for lawful purposes by individuals for
personal, non-commercial use only"; no material may be "copied, modified, reproduced, republished ... or distributed".
Collection waits for the owner's decision (data_rights.PENDING["nifty_indices_press"]).
"""

from __future__ import annotations

import html as htmllib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

SITE = "https://www.niftyindices.com"
PAGE = SITE + "/press-release"
LOOKBACK_DAYS = 120  # a semi-annual review is announced about four to six weeks ahead
MIN_ITEMS = 5  # the real page lists ~1,500 releases; a handful or none means a block page or a changed layout
TITLE_NOTE = "index funds trade it at the close of the session before"


class NiftyIndicesError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    published: date
    url: str
    title: str


_ITEM = re.compile(r"<div\s+class=\"pressItem\"\s+data-date=\"([^\"]+)\"[^>]*>.*?<a\s+href=['\"]([^'\"]+)['\"][^>]*>"
                   r"(.*?)</a>", re.S | re.I)  # fmt: skip
_CHANGE = re.compile(r"\b(replacements?|inclusions?|exclusions?|changes?)\b", re.I)
_OTHER_FAMILIES = re.compile(r"fixed\s+income|\bdebt\b|\bbonds?\b|g-?sec|\bsdl\b|t-?bill|treasury|\bgilt\b|\bcd\b|"
                             r"commercial\s+paper|certificate\s+of\s+deposit|money\s+market|\bipo\b|\bsme\b|\bemerge\b",
                             re.I)  # fmt: skip
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_WEF = re.compile(
    r"w\.?\s*e\.?\s*f\.?\s*([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*,?\s*(\d{4})", re.I
)  # "July 01 2026" too


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _url(href: str) -> str:
    href = htmllib.unescape(href.strip())
    if href.startswith("/"):
        return SITE + href
    if href.startswith(SITE + "/"):
        return href
    raise NiftyIndicesError(f"NSE Indices press release linked off the site: {href!r}")


def parse_listing(page: str) -> list[Release]:
    out = []
    for d, href, title in _ITEM.findall(page):
        try:
            published = datetime.strptime(d.strip(), "%b %d, %Y").date()
        except ValueError as e:
            raise NiftyIndicesError(f"NSE Indices press release with a bad date {d!r}") from e
        out.append(Release(published=published, url=_url(href), title=_text(title)))
    return out


def is_candidate(title: str) -> bool:
    """A constituent change of an equity index family that F&O plans read."""
    return bool(_CHANGE.search(title)) and not _OTHER_FAMILIES.search(title)


def effective_date(title: str) -> date | None:
    m = _WEF.search(title)
    if not m or m.group(1)[:3].lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS.index(m.group(1)[:3].lower()) + 1, int(m.group(2)))
    except ValueError:
        return None


def flow_day(wef: date, holidays: set[date] | None) -> tuple[date, bool]:
    """The session before `wef` and whether it is exact (False: that year's NSE holiday list is unknown, so only
    weekends were skipped)."""
    from finresearch.fincalc.dates import add_business_days

    return add_business_days(wef, -1, holidays or set()), holidays is not None


def _holidays(day: date) -> set[date] | None:
    from finresearch.adapters.nse_holidays import known_trading_holidays

    return known_trading_holidays(day)


async def read(known, now, *, http=None, holidays_for: Callable[[date], set[date] | None] = _holidays):
    """Future rebalances announced in the last LOOKBACK_DAYS. Any refusal or unparsable recent candidate raises
    (nothing is stored)."""
    from finresearch.adapters.http import PoliteClient
    from finresearch.eventcal.calendar import TITLES, Event
    from finresearch.eventcal.refresh import Found
    from finresearch.fincalc.dates import to_ist

    today = to_ist(now).date()
    own = http is None
    http = http or PoliteClient(cache_dir=None)
    try:
        got = await http.get(PAGE)
    finally:
        if own:
            await http.aclose()
    items = parse_listing(got.text) if got.ok else []
    if len(items) < MIN_ITEMS:
        raise NiftyIndicesError(
            f"NSE Indices press-release page: HTTP {got.status}, {len(items)} releases listed"
        )
    found = Found("nifty_indices_press", urls=[PAGE])
    since = today - timedelta(days=LOOKBACK_DAYS)
    for r in items:
        if r.published < since or not is_candidate(r.title):
            continue
        wef = effective_date(r.title)
        if (
            wef is None
        ):  # 3-5 a year (e.g. "Replacements in indices", 25-Sep-2024): read the release, never guess
            found.notices.append(f"NSE Indices press release of {r.published} has no effective date in its title; "
                                 f"read it: {r.title} ({r.url})")  # fmt: skip
            continue
        day, exact = flow_day(wef, holidays_for(wef - timedelta(days=1)))
        if day < today:
            continue
        note = TITLE_NOTE + ("" if exact else "; approximate: that year's NSE holiday list is unknown")
        found.events.append(Event(type="index_rebalance", tier=2, scope="IN", day=day, at=None,
                                  title=f"{TITLES['index_rebalance']}: {r.title} ({note})", source_url=r.url,
                                  known_since=now, known_since_url=r.url))  # fmt: skip
    return found

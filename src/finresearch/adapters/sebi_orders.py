"""SEBI enforcement orders from SEBI's public RSS feed (https://www.sebi.gov.in/sebirss.xml), matched against the
names of companies the user holds or watches.

The feed (checked 01-Oct-2026: HTTP 200, text/xml, ~16 KB) carries the latest ~30 items, each with title, description,
link and pubDate ("30 Sep, 2026 +0530"). Enforcement items link under /enforcement/ (orders: adjudication,
settlement, final; recovery proceedings), alongside press releases and circulars.

Privacy: many titles name private individuals and some print a PAN ("against <name> (PAN: ABCDE1234F)"). So:
- every title is scrubbed of PAN / Aadhaar-like / DIN identifiers (`nse_disclosures.scrub`) before anything else;
- only items whose title matches a tracked company's legal name are kept (`match_orders`); the caller stores those
  and nothing else. An unmatched title is never stored or shown.

Matching is deliberately strict and labelled "possible match": the company's normalised legal name must appear as a
whole run of words, followed by a legal suffix, the end, or punctuation / a joining word. "Example Ltd" does not
match "Example Holdings Ltd", and a one-word name only matches when a legal suffix ("Ltd", "Limited") follows it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from xml.etree import ElementTree as ET

from pydantic import BaseModel

from finresearch.adapters.http import Fetched, PoliteClient
from finresearch.adapters.nse_disclosures import parse_day, scrub

RSS_URL = "https://www.sebi.gov.in/sebirss.xml"
MAX_RSS_BYTES = 2_000_000


class SebiError(RuntimeError):
    pass


class SebiOrder(BaseModel):
    title: str  # PAN-scrubbed
    link: str
    day: date | None
    kind: str  # "adjudication" | "settlement" | "final" | "recovery" | "order" | "other"


class OrderMatch(BaseModel):
    order: SebiOrder
    name: str  # the tracked company name that matched
    key: str  # the tracked instrument key (NSE symbol, "BSE:<code>", or a bond ISIN)


def _kind(title: str, link: str) -> str:
    low = f"{title} {link}".lower()
    for k in ("adjudication", "settlement", "recovery", "final"):
        if k in low:
            return k
    return "order" if "/enforcement/" in link else "other"


def parse_rss(content: bytes) -> tuple[list[SebiOrder], datetime | None]:
    """The RSS body -> enforcement items (titles scrubbed) and the feed's lastBuildDate. Non-enforcement items (press
    releases, circulars) are dropped."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise SebiError(f"SEBI RSS is not well-formed XML: {e}") from e
    channel = root.find("channel")
    if channel is None:
        raise SebiError("SEBI RSS has no <channel> (not the feed?)")
    built = None
    lb = (channel.findtext("lastBuildDate") or "").strip()
    for fmt in ("%d %b %Y %H:%M:%S", "%a, %d %b %Y %H:%M:%S"):
        try:
            built = datetime.strptime(lb[:20] if fmt.startswith("%d") else lb[:25], fmt)
            break
        except ValueError:
            continue
    out = []
    for it in channel.findall("item"):
        link = (it.findtext("link") or "").strip()
        if "/enforcement/" not in link or not link.startswith("https://www.sebi.gov.in/"):
            continue
        title = scrub(it.findtext("title") or it.findtext("description") or "") or ""
        out.append(
            SebiOrder(title=title, link=link, day=parse_day(it.findtext("pubDate")), kind=_kind(title, link))
        )
    return out, built


# --------------------------------------------------------------------------- name matching
_SUFFIX = {"ltd", "limited", "pvt", "private", "plc", "inc"}
_JOIN = {
    "in",
    "and",
    "for",
    "of",
    "the",
    "group",
    "now",
    "formerly",
    "erstwhile",
    "to",
    "with",
    "under",
    "by",
    "a",
}
_NORM = (("&", " and "), ("limited", "ltd"), ("private", "pvt"), ("(india)", "india"), ("co.", "co"))


def tokens(name: str) -> list[str]:
    low = f" {name.lower()} "
    for a, b in _NORM:
        low = low.replace(a, b)
    return re.findall(r"[a-z0-9]+", low)


def core(name: str) -> list[str]:
    """The name's words without trailing legal suffixes: "Example Holdings Ltd." -> ["example", "holdings"]."""
    t = tokens(name)
    while t and t[-1] in _SUFFIX:
        t.pop()
    return t


def name_in_title(name: str, title: str) -> bool:
    """Whether a company's legal name appears in an order title (see the module doc for the rule)."""
    c = core(name)
    if not c or (len(c) == 1 and len(c[0]) < 4):
        return False
    tt = tokens(title)
    n = len(c)
    for i in range(len(tt) - n + 1):
        if tt[i : i + n] != c:
            continue
        nxt = tt[i + n] if i + n < len(tt) else None
        if len(c) == 1:
            if nxt in _SUFFIX:
                return True
            continue
        if nxt is None or nxt in _SUFFIX or nxt in _JOIN:
            return True
    return False


def match_orders(orders: list[SebiOrder], companies: dict[str, str]) -> list[OrderMatch]:
    """Orders whose title names one of `companies` ({instrument key: legal name}). One match per (order, key)."""
    out = []
    for o in orders:
        for key, name in companies.items():
            if name and name_in_title(name, o.title):
                out.append(OrderMatch(order=o, name=name, key=key))
    return out


async def fetch_orders(
    client: PoliteClient | None = None,
) -> tuple[list[SebiOrder], datetime | None, Fetched]:
    """GET the RSS (no disk cache: the monitor reads it once a day and the database keeps the matches)."""
    own = client is None
    http = client or PoliteClient()
    try:
        resp = await http.get(RSS_URL)
    finally:
        if own:
            await http.aclose()
    if not resp.ok:
        raise SebiError(f"SEBI HTTP {resp.status} for {RSS_URL}")
    if resp.record.size > MAX_RSS_BYTES:
        raise SebiError(f"SEBI RSS is {resp.record.size} bytes, over the {MAX_RSS_BYTES} cap")
    orders, built = parse_rss(resp.content)
    return orders, built, resp

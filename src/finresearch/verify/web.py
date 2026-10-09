"""Web page snapshots for evidence grading (#242).

An agent that relies on a web page fetches it with the `fetch_page` MCP tool. The tool stores the page TEXT it
returned as a `WebSnapshot` (sha256 of that text), and `save_claim` checks a web citation's quote against the latest
snapshot of the same URL: the citation then carries `quote_found` and `snapshot_sha256` (grade B, or U when the quote
is not on the page). Without a snapshot the quote is unchecked (grade C). Exchange tools store their JSON responses
under their `source` URL (run_id None), so a figure quoted from them is checked the same way.

The fetcher is injectable (`FETCHER`) so tests use recorded pages and never touch the network.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from finresearch.db.models import WebSnapshot

MAX_CHARS = 2_000_000  # a page's stored text; a longer page is refused rather than cut (invariant 7a)
PAGE_CHARS = 20_000  # characters per fetch_page call; the rest is paged with `offset`
TOOL_SNAPSHOT_AGE = timedelta(hours=24)  # an exchange tool's response (run_id None) counts for a day
_SKIP = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK = {
    "p",
    "div",
    "br",
    "li",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "section",
    "article",
    "td",
    "th",
}

# (url) -> (text or html, content type); replaced in tests
FETCHER: Callable[[str], Awaitable[tuple[str, str]]] | None = None


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    """Visible text of an HTML page, one block per line (scripts, styles and the head dropped)."""
    p = _Text()
    p.feed(html)
    p.close()
    lines = (re.sub(r"[ \t ]+", " ", ln).strip() for ln in "".join(p.out).splitlines())
    return "\n".join(ln for ln in lines if ln)


def page_text(body: str, content_type: str) -> str:
    return html_to_text(body) if "html" in (content_type or "").lower() or body.lstrip()[:1] == "<" else body


async def _fetch(url: str) -> tuple[str, str]:
    """Every hop's host is resolved and must be public, and the body is capped (adapters.http.fetch_public, #259)."""
    from finresearch.adapters.http import fetch_public

    r = await fetch_public(url)
    if not r.ok:
        raise ValueError(f"HTTP {r.status} for {url}")
    return r.text, r.record.content_type or ""


async def fetch(url: str) -> tuple[str, str]:
    """(page text, content type). Raises UnsafeURLError for a private / non-http(s) URL and ValueError on failure."""
    from finresearch.adapters.http import check_public_url

    check_public_url(url)
    body, ctype = await (FETCHER or _fetch)(url)
    text = page_text(body, ctype)
    if len(text) > MAX_CHARS:
        raise ValueError(
            f"page text is {len(text):,} characters, over the {MAX_CHARS:,} limit; cite another source"
        )
    return text, ctype


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def store(
    session: Session, url: str, text: str, *, run_id: int | None, content_type: str | None = None
) -> WebSnapshot:
    """Record a page's text (an identical text already stored for this run and URL is reused)."""
    h = sha(text)
    old = session.scalar(select(WebSnapshot).where(WebSnapshot.url == url, WebSnapshot.sha256 == h,
                                                   WebSnapshot.run_id.is_(None) if run_id is None
                                                   else WebSnapshot.run_id == run_id))  # fmt: skip
    if old is not None:
        old.fetched_at = datetime.now(UTC)
        return old
    snap = WebSnapshot(run_id=run_id, url=url, sha256=h, content_type=(content_type or "")[:120] or None,
                       text=text, fetched_at=datetime.now(UTC))  # fmt: skip
    session.add(snap)
    session.flush()
    return snap


def latest(session: Session, url: str, run_id: int, now: datetime | None = None) -> WebSnapshot | None:
    """The newest snapshot of `url` this run fetched, or an exchange tool's response from the last day."""
    now = now or datetime.now(UTC)
    q = select(WebSnapshot).where(WebSnapshot.url == url, or_(
        WebSnapshot.run_id == run_id,
        (WebSnapshot.run_id.is_(None)) & (WebSnapshot.fetched_at >= now - TOOL_SNAPSHOT_AGE)))  # fmt: skip
    return session.scalars(q.order_by(WebSnapshot.fetched_at.desc(), WebSnapshot.id.desc())).first()

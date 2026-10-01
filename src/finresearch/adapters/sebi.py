"""SEBI adapter: public-issue offer-document filings and their PDF links.

SEBI has no API for filings; the "Filings > Public Issues" listing is server-rendered HTML
(verified live 28-Sep-2026):

- Page 1: GET  /sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=<smid>
- Page N: POST /sebiweb/ajax/home/getnewslistinfo.jsp (form fields mirror the page's JS
  `searchFormNewsList('n', N-1)`); the response is "<rows html>#@#<breadcrumb html>" and needs the
  JSESSIONID cookie from the page-1 GET.
- smid (sub-sub-section): 10 = Draft Offer Documents filed with SEBI (DRHP/UDRHP/addenda),
  11 = Red Herring Documents filed with ROC (RHP + corrigenda), 12 = Final Offer Documents (Prospectus),
  78 = Other Documents.

Each listing row links to a detail page whose `<iframe src="../../../web/?file=<pdf>">` points at the
full document (sebi_data/attachdocs/...pdf); the row itself only carries the *abridged* prospectus
(sebi_data/commondocs/..._p.pdf). `resolve_pdf_url` returns the full document first.

SEBI's RSS feed (/sebirss.xml) was checked and carries orders/press releases only, no public-issue
filings, so it is not used here.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import parse_qs, urljoin, urlsplit

from pydantic import BaseModel

from finresearch.adapters.http import PoliteClient

SEBI_BASE = "https://www.sebi.gov.in"
SEBI_HOSTS = frozenset({"www.sebi.gov.in", "sebi.gov.in"})
LISTING_URL = f"{SEBI_BASE}/sebiweb/home/HomeAction.do"
AJAX_LISTING_URL = f"{SEBI_BASE}/sebiweb/ajax/home/getnewslistinfo.jsp"

FilingKind = Literal["drhp", "rhp", "prospectus", "other"]
KIND_SMID: dict[str, int] = {"drhp": 10, "rhp": 11, "prospectus": 12, "other": 78}
KIND_LABEL: dict[str, str] = {
    "drhp": "Draft Offer Documents filed with SEBI",
    "rhp": "Red Herring Documents filed with ROC",
    "prospectus": "Final Offer Documents filed with ROC",
    "other": "Other Documents",
}
PUBLIC_ISSUES_SID, PUBLIC_ISSUES_SSID = 3, 15


class SebiError(RuntimeError):
    pass


class Filing(BaseModel):
    date: date
    title: str  # whitespace-normalised, e.g. "Orient Cables (India) Limited - RHP"
    company: str
    doc_kind: str | None  # free text after the last " - ": "DRHP", "UDRHP-I", "RHP", "Addendum to DRHP", ...
    listing: FilingKind  # which SEBI listing it came from
    detail_url: str
    filing_id: int | None = None  # SEBI's numeric id from "..._104663.html"
    abridged_pdf_url: str | None = None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _abs_url(base: str, href: str) -> str:
    """Resolve relative links; SEBI sometimes emits literal spaces in PDF paths."""
    return urljoin(base, href.strip()).replace(" ", "%20")


def split_title(title: str) -> tuple[str, str | None]:
    """'JSW One Platforms Limited - DRHP' -> ('JSW One Platforms Limited', 'DRHP')."""
    if " - " in title:
        company, kind = title.rsplit(" - ", 1)
        return company.strip(), kind.strip() or None
    return title, None


_ID = re.compile(r"_(\d+)\.html?$")


class _ListingParser(HTMLParser):
    """Rows look like <tr><td>Sep 25, 2026</td><td><a href=DETAIL class=points>TITLE<br><a href=ABRIDGED>..</a></a>.

    Page-1 HTML and the ajax fragment differ in quoting and in missing </td>, so we key off tags,
    not regexes.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str | None]] = []
        self._cells: list[str] = []
        self._in_td = False
        self._a_depth = 0
        self._after_br = False
        self._cur: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "tr":
            self._flush()
            self._cur = {"date": None, "href": None, "title": "", "abridged": None}
            self._cells = []
        elif tag == "td" and self._cur is not None:
            self._in_td = True
            self._cells.append("")
            self._a_depth = 0
            self._after_br = False
        elif tag == "a" and self._cur is not None and self._in_td:
            href = (a.get("href") or "").strip()
            self._a_depth += 1
            if self._a_depth == 1 and self._cur["href"] is None and "/filings/" in href:
                self._cur["href"] = href
            elif self._a_depth > 1 and href and self._cur["abridged"] is None:
                self._cur["abridged"] = href
        elif tag == "br" and self._a_depth:
            self._after_br = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._a_depth:
            self._a_depth -= 1
        elif tag == "td":
            self._in_td = False
        elif tag in ("tr", "tbody", "table"):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._cur is None or not self._in_td or not self._cells:
            return
        self._cells[-1] += data
        if self._a_depth == 1 and not self._after_br and self._cur["href"] is not None:
            self._cur["title"] = (self._cur["title"] or "") + data

    def _flush(self) -> None:
        cur = self._cur
        if cur is not None and cur["href"] and self._cells:
            cur["date"] = _norm(self._cells[0])
            self.rows.append(cur)
        self._cur = None
        self._cells = []
        self._in_td = False
        self._a_depth = 0

    def close(self) -> None:
        super().close()
        self._flush()


def parse_listing(html: str, listing: FilingKind) -> list[Filing]:
    """Parse a listing page (full page or the ajax fragment; the '#@#' breadcrumb tail is ignored)."""
    parser = _ListingParser()
    parser.feed(html.split("#@#", 1)[0])
    parser.close()
    filings: list[Filing] = []
    for row in parser.rows:
        try:
            when = datetime.strptime(row["date"] or "", "%b %d, %Y").date()
        except ValueError:
            continue  # header or malformed row
        detail = urljoin(SEBI_BASE + "/", row["href"] or "")
        title = _norm(row["title"] or "")
        company, kind = split_title(title)
        m = _ID.search(urlsplit(detail).path)
        abridged = row["abridged"]
        filings.append(
            Filing(
                date=when,
                title=title,
                company=company,
                doc_kind=kind,
                listing=listing,
                detail_url=detail,
                filing_id=int(m.group(1)) if m else None,
                abridged_pdf_url=_abs_url(detail, abridged) if abridged else None,
            )
        )
    return filings


_RECORDS = re.compile(r"(\d+)\s+to\s+(\d+)\s+of\s+(\d+)\s+records", re.IGNORECASE)


def parse_total_records(html: str) -> int | None:
    m = _RECORDS.search(html)
    return int(m.group(3)) if m else None


class _PdfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.iframes: list[str] = []
        self.anchors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("iframe", "embed") and a.get("src"):
            self.iframes.append(a["src"] or "")
        elif tag == "object" and a.get("data"):
            self.iframes.append(a["data"] or "")
        elif tag == "a" and a.get("href"):
            self.anchors.append(a["href"] or "")


def parse_detail_pdfs(html: str, detail_url: str) -> list[str]:
    """PDF URLs on a filing detail page: full document (iframe `file=`) first, then abridged/other PDFs."""
    parser = _PdfParser()
    parser.feed(html)
    parser.close()
    out: list[str] = []

    def add(url: str) -> None:
        url = _abs_url(detail_url, url)
        parts = urlsplit(url)
        # the page is untrusted input: only SEBI's own documents are offered for download (a link to another host
        # would be fetched as "the offer document" and cited as SEBI's)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in SEBI_HOSTS:
            return
        if url not in out:
            out.append(url)

    for src in parser.iframes:
        target = parse_qs(urlsplit(src).query).get("file")
        if target:
            add(target[0])
        elif src.lower().split("?")[0].endswith(".pdf"):
            add(src)
    for href in parser.anchors:
        low = href.lower()
        if "sebi_data/" in low and (".pdf" in low or "commondocs" in low or "attachdocs" in low):
            add(href)
    # Title/meta attributes embed the abridged link as escaped HTML; catch it if no anchor did.
    for m in re.finditer(r"https?://www\.sebi\.gov\.in/sebi_data/[^'\"<>]+?\.pdf", html, re.IGNORECASE):
        add(m.group(0))
    return out


class SebiClient:
    """SEBI public-issue filings. Owns its PoliteClient (1 req/s) unless one is passed in."""

    def __init__(self, client: PoliteClient | None = None) -> None:
        self._own = client is None
        self.http = client or PoliteClient()

    async def __aenter__(self) -> SebiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own:
            await self.http.aclose()

    async def list_public_issue_filings(
        self, kind: Literal["drhp", "rhp", "prospectus", "other", "all"] = "rhp", pages: int = 1
    ) -> list[Filing]:
        """Newest-first filings. `all` = DRHP + RHP + Prospectus listings merged and sorted by date."""
        if kind == "all":
            merged: list[Filing] = []
            for k in ("drhp", "rhp", "prospectus"):
                merged.extend(await self.list_public_issue_filings(k, pages))  # type: ignore[arg-type]
            return sorted(merged, key=lambda f: (f.date, f.filing_id or 0), reverse=True)
        if kind not in KIND_SMID:
            raise ValueError(f"unknown filing kind {kind!r}")
        smid = KIND_SMID[kind]
        page1_url = (
            f"{LISTING_URL}?doListing=yes&sid={PUBLIC_ISSUES_SID}&ssid={PUBLIC_ISSUES_SSID}&smid={smid}"
        )
        resp = await self.http.get(page1_url)
        if not resp.ok:
            raise SebiError(f"SEBI listing HTTP {resp.status}: {page1_url}")
        filings = parse_listing(resp.text, kind)
        for page in range(2, pages + 1):
            resp = await self.http.post(
                AJAX_LISTING_URL,
                data=self._page_form(kind, smid, page),
                headers={"Referer": page1_url},
            )
            if not resp.ok:
                raise SebiError(f"SEBI listing page {page} HTTP {resp.status}")
            batch = parse_listing(resp.text, kind)
            if not batch:
                break
            filings.extend(batch)
        return filings

    @staticmethod
    def _page_form(kind: str, smid: int, page: int) -> dict[str, str]:
        return {
            "nextValue": "1",
            "next": "n",
            "search": "",
            "fromDate": "",
            "toDate": "",
            "fromYear": "",
            "toYear": "",
            "deptId": "",
            "sid": str(PUBLIC_ISSUES_SID),
            "ssid": str(PUBLIC_ISSUES_SSID),
            "smid": str(smid),
            "ssidhidden": str(PUBLIC_ISSUES_SSID),
            "intmid": "-1",
            "sText": "Filings",
            "ssText": "Public Issues",
            "smText": KIND_LABEL[kind],
            "doDirect": str(page - 1),
        }

    async def resolve_pdf_url(self, detail_url: str) -> list[str]:
        """Fetch a filing detail page and return its PDF link(s), full document first."""
        resp = await self.http.get(detail_url)
        if not resp.ok:
            raise SebiError(f"SEBI detail HTTP {resp.status}: {detail_url}")
        return parse_detail_pdfs(resp.text, detail_url)

"""NSE listed-equity data: price history, corporate announcements, results filings, shareholding and corporate
actions. Every call warms the quote page first and sends it as referer (NSE refuses scripted clients otherwise).

Results filings come from two NSE indexes (verified live 29-Sep-2026):
- `/api/corporates-financial-results` (the "Financial Results" page): Regulation 33 filings up to the Dec-2024
  quarter. It gets no new quarters.
- `/api/integrated-filing-results` (the "Integrated Filing" page): from the Mar-2025 quarter SEBI moved quarterly
  financial results into Integrated Filing (Financials). Each row links the results XBRL (same Ind AS facts, a
  newer "in-capmkt" namespace; banks use a banking taxonomy) and its iXBRL rendering. The listing also carries
  "Integrated Filing- Governance" rows; `integrated_filings()` keeps only the financials.

Recorded payloads live in tests/fixtures/nse/equity/. Dates in the payloads are NSE's (IST).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from finresearch.adapters.nse import (
    API_HEADERS,
    NSE_BASE,
    NseClient,
    NseError,
    parse_nse_date,
    parse_nse_timestamp,
    parse_num,
)
from finresearch.fincalc.market import dividend_per_share


class PriceBar(BaseModel):
    day: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    prev_close: Decimal | None
    vwap: Decimal | None
    volume: Decimal | None
    value_inr: Decimal | None
    trades: Decimal | None
    week52_high: Decimal | None = None
    week52_low: Decimal | None = None

    @classmethod
    def parse(cls, r: dict[str, Any]) -> PriceBar:
        return cls(day=parse_nse_date(r.get("mtimestamp")), open=parse_num(r.get("chOpeningPrice")),
                   high=parse_num(r.get("chTradeHighPrice")), low=parse_num(r.get("chTradeLowPrice")),
                   close=parse_num(r.get("chClosingPrice")), prev_close=parse_num(r.get("chPreviousClsPrice")),
                   vwap=parse_num(r.get("vwap")), volume=parse_num(r.get("chTotTradedQty")),
                   value_inr=parse_num(r.get("chTotTradedVal")), trades=parse_num(r.get("chTotalTrades")),
                   week52_high=parse_num(r.get("ch52WeekHighPrice")), week52_low=parse_num(r.get("ch52WeekLowPrice")))  # fmt: skip


class IndexBar(BaseModel):
    """One day of an NSE index (price index; NSE's history API has no total-return series)."""

    day: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None

    @classmethod
    def parse(cls, r: dict[str, Any]) -> IndexBar:
        return cls(day=_upper_date(r.get("EOD_TIMESTAMP")), open=parse_num(r.get("EOD_OPEN_INDEX_VAL")),
                   high=parse_num(r.get("EOD_HIGH_INDEX_VAL")), low=parse_num(r.get("EOD_LOW_INDEX_VAL")),
                   close=parse_num(r.get("EOD_CLOSE_INDEX_VAL")))  # fmt: skip


class Announcement(BaseModel):
    symbol: str
    at: datetime | None
    category: str
    text: str
    attachment: str | None
    results_period_end: date | None = None  # set when the filing says it contains financial results

    @classmethod
    def parse(cls, r: dict[str, Any]) -> Announcement:
        text = (r.get("attchmntText") or "").strip()
        m = re.search(
            r"financial results for the (?:period|quarter|year) ended\s+([A-Za-z]+\.? \d{1,2},? \d{4})",
            text,
            re.I,
        )
        end = None
        if m:
            for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
                try:
                    end = datetime.strptime(m.group(1).replace(".", ""), fmt).date()
                    break
                except ValueError:
                    continue
        att = r.get("attchmntFile") or None
        return cls(symbol=r.get("symbol", ""), at=parse_nse_timestamp(r.get("an_dt")), category=r.get("desc") or "",
                   text=text, attachment=att if att and att.startswith("http") else None, results_period_end=end)  # fmt: skip


RESULTS_PAGE = f"{NSE_BASE}/companies-listing/corporate-filings-financial-results"
INTEGRATED_PAGE = f"{NSE_BASE}/companies-listing/corporate-integrated-filing"
INTEGRATED_FINANCIALS = "Integrated Filing- Financials"


class ResultFiling(BaseModel):
    symbol: str
    period_from: date | None
    period_to: date | None
    relating_to: str | None
    consolidated: bool
    audited: bool | None
    filed_at: datetime | None
    xbrl: str | None
    source: str = "nse_financial_results"  # or "nse_integrated_filing"
    ixbrl: str | None = None  # human-readable rendering of the XBRL (integrated filings only)
    revised: bool = False

    @classmethod
    def parse(cls, r: dict[str, Any]) -> ResultFiling:
        aud = (r.get("audited") or "").lower()
        return cls(symbol=r.get("symbol", ""), period_from=parse_nse_date(r.get("fromDate")),
                   period_to=parse_nse_date(r.get("toDate")), relating_to=r.get("relatingTo"),
                   consolidated=(r.get("consolidated") or "").lower() == "consolidated",
                   audited=None if not aud else aud == "audited", filed_at=parse_nse_timestamp(r.get("filingDate")),
                   xbrl=r.get("xbrl") or None)  # fmt: skip


def _archive_link(v: Any) -> str | None:
    """NSE sends 'https://nsearchives.nseindia.com/corporate/null' (or null) when a filing has no such file."""
    v = (v or "").strip()
    return v if v.startswith("https://") and not v.rstrip("/").endswith("/null") else None


def _quarter_start(end: date) -> date:
    m = end.month - 2
    return date(end.year if m > 0 else end.year - 1, m if m > 0 else m + 12, 1)


class IntegratedFiling(BaseModel):
    """One row of NSE's integrated-filing index (financials or governance)."""

    symbol: str
    kind: str  # "Integrated Filing- Financials" | "Integrated Filing- Governance"
    period_end: date | None
    consolidated: bool | None
    audited: bool | None
    filed_at: datetime | None
    revised_at: datetime | None
    sub_type: str | None  # Original | New | Revised
    xbrl: str | None
    ixbrl: str | None
    pdf: str | None

    @classmethod
    def parse(cls, r: dict[str, Any]) -> IntegratedFiling:
        nature = (r.get("consolidated") or "").lower()
        aud = (r.get("audited") or "").lower()
        return cls(symbol=r.get("symbol", ""), kind=r.get("type") or "", period_end=_upper_date(r.get("qe_Date")),
                   consolidated=None if not nature else nature.startswith("consolidated"),
                   audited=None if not aud else aud == "audited", filed_at=parse_nse_timestamp(r.get("broadcast_Date")),
                   revised_at=parse_nse_timestamp(r.get("revised_Date")), sub_type=r.get("type_Sub"),
                   xbrl=_archive_link(r.get("xbrl")), ixbrl=_archive_link(r.get("ixbrl")),
                   pdf=_archive_link(r.get("pdf_attach")))  # fmt: skip

    def as_result_filing(self) -> ResultFiling:
        """The same shape as a Financial Results index row, so both indexes merge into one list."""
        return ResultFiling(symbol=self.symbol, period_from=_quarter_start(self.period_end) if self.period_end else None,
                            period_to=self.period_end, relating_to=None, consolidated=bool(self.consolidated),
                            audited=self.audited, filed_at=self.revised_at or self.filed_at, xbrl=self.xbrl,
                            source="nse_integrated_filing", ixbrl=self.ixbrl,
                            revised=(self.sub_type or "").lower().startswith("revis"))  # fmt: skip


class Shareholding(BaseModel):
    symbol: str
    as_of: date | None
    promoter_pct: Decimal | None
    public_pct: Decimal | None
    employee_trusts_pct: Decimal | None
    submitted: date | None
    xbrl: str | None

    @classmethod
    def parse(cls, r: dict[str, Any]) -> Shareholding:
        return cls(symbol=r.get("symbol", ""), as_of=_upper_date(r.get("date")),
                   promoter_pct=parse_num(r.get("pr_and_prgrp")), public_pct=parse_num(r.get("public_val")),
                   employee_trusts_pct=parse_num(r.get("employeeTrusts")), submitted=_upper_date(r.get("submissionDate")),
                   xbrl=r.get("xbrl") or None)  # fmt: skip


class CorporateAction(BaseModel):
    symbol: str
    subject: str
    ex_date: date | None
    record_date: date | None
    dividend_per_share: Decimal | None

    @classmethod
    def parse(cls, r: dict[str, Any]) -> CorporateAction:
        subj = r.get("subject") or ""
        return cls(symbol=r.get("symbol", ""), subject=subj, ex_date=parse_nse_date(r.get("exDate")),
                   record_date=parse_nse_date(r.get("recDate")), dividend_per_share=dividend_per_share(subj))  # fmt: skip


class AnnualReportFiling(BaseModel):
    symbol: str
    from_year: int | None
    to_year: int | None
    url: str
    submission: str | None  # New | Revised
    at: datetime | None

    @property
    def fiscal_label(self) -> str:
        return f"FY{str(self.to_year)[-2:]}" if self.to_year else "FY?"

    @classmethod
    def parse(cls, r: dict[str, Any], symbol: str) -> AnnualReportFiling:
        def year(v: Any) -> int | None:
            try:
                return int(str(v))
            except (TypeError, ValueError):
                return None

        return cls(symbol=symbol, from_year=year(r.get("fromYr")), to_year=year(r.get("toYr")),
                   url=r.get("fileName") or "", submission=r.get("submission_type"),
                   at=parse_nse_timestamp((r.get("broadcast_dttm") or "").title() or None))  # fmt: skip


def latest_annual_reports(filings: list[AnnualReportFiling], years: int = 2) -> list[AnnualReportFiling]:
    """The most recent filing per financial year (a revised filing replaces the original), newest years first."""
    best: dict[int, AnnualReportFiling] = {}
    for f in filings:
        if not f.to_year or not f.url.startswith("http"):
            continue
        cur = best.get(f.to_year)
        if cur is None or (f.at and cur.at and f.at > cur.at):
            best[f.to_year] = f
    return [best[y] for y in sorted(best, reverse=True)[:years]]


def _upper_date(v: str | None) -> date | None:
    """'30-JUN-2026' -> date (shareholding payloads use upper-case months)."""
    return parse_nse_date(v.title()) if v and v != "-" else None


class NseEquity:
    """Listed-equity endpoints on top of an NseClient."""

    def __init__(self, client: NseClient | None = None):
        self.nse = client or NseClient()

    async def __aenter__(self) -> NseEquity:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.nse.aclose()

    async def _get(
        self, symbol: str, path: str, params: dict[str, str], *, cache_ttl: float | None = None
    ) -> Any:
        """GET with the quote page as referer. A stale session (401/403 or an HTML block page) is re-warmed once.
        With `cache_ttl`, only JSON bodies are cached (for data that no longer changes, such as old price bars)."""
        page = f"{NSE_BASE}/get-quotes/equity?symbol={symbol}"
        for attempt in range(2):
            if not self.nse._warmed:
                self.nse.http.cookies.clear()
                await self.nse.http.get(page)
                self.nse._warmed = True
            resp = await self.nse.http.get(
                f"{NSE_BASE}{path}", params=params, headers={**API_HEADERS, "Referer": page},
                cache_ttl=cache_ttl, cache_if=_json_body if cache_ttl is not None else None,
            )  # fmt: skip
            stale = resp.status in (401, 403) or (resp.ok and not _json_body(resp))
            if stale and attempt == 0:
                self.nse._warmed = False
                continue
            break
        if not resp.ok:
            raise NseError(f"NSE HTTP {resp.status} for {path} ({symbol})")
        try:
            return resp.json()
        except ValueError as e:
            raise NseError(f"NSE returned a non-JSON page for {path} ({symbol})") from e

    async def history(self, symbol: str, start: date, end: date, series: str = "EQ", *,
                      cache_ttl: float | None = None) -> list[PriceBar]:  # fmt: skip
        rows = await self._get(symbol, "/api/NextApi/apiClient/GetQuoteApi",
                               {"functionName": "getHistoricalTradeData", "symbol": symbol, "series": series,
                                "fromDate": start.strftime("%d-%m-%Y"), "toDate": end.strftime("%d-%m-%Y")},
                               cache_ttl=cache_ttl)  # fmt: skip
        return sorted((PriceBar.parse(r) for r in rows or []), key=lambda b: b.day)

    async def index_history(self, index: str, start: date, end: date) -> list[IndexBar]:
        """Daily values of an NSE index (e.g. "NIFTY 50"); like `history`, one request answers ~70 trading days."""
        d = await self._get("INFY", "/api/historicalOR/indicesHistory",
                            {"indexType": index, "from": start.strftime("%d-%m-%Y"), "to": end.strftime("%d-%m-%Y")})  # fmt: skip
        rows = d.get("data", []) if isinstance(d, dict) else d or []
        return sorted((b for b in (IndexBar.parse(r) for r in rows) if b.day), key=lambda b: b.day)

    async def announcements(self, symbol: str) -> list[Announcement]:
        rows = await self._get(
            symbol, "/api/corporate-announcements", {"index": "equities", "symbol": symbol}
        )
        return [Announcement.parse(r) for r in rows or []]

    async def results(self, symbol: str, period: str = "Quarterly") -> list[ResultFiling]:
        rows = await self._get(symbol, "/api/corporates-financial-results",
                               {"index": "equities", "symbol": symbol, "period": period})  # fmt: skip
        return [ResultFiling.parse(r) for r in rows or []]

    async def integrated_filings(
        self, symbol: str, kind: str = INTEGRATED_FINANCIALS
    ) -> list[IntegratedFiling]:
        """Integrated filings of one kind, newest first (NSE's own order)."""
        d = await self._get(symbol, "/api/integrated-filing-results",
                            {"index": "equities", "symbol": symbol, "type": kind})  # fmt: skip
        rows = d.get("data", []) if isinstance(d, dict) else d or []
        return [f for f in (IntegratedFiling.parse(r) for r in rows) if f.kind == kind]

    async def shareholding(self, symbol: str) -> list[Shareholding]:
        rows = await self._get(
            symbol, "/api/corporate-share-holdings-master", {"index": "equities", "symbol": symbol}
        )
        return sorted(
            (Shareholding.parse(r) for r in rows or []), key=lambda x: x.as_of or date.min, reverse=True
        )

    async def corporate_actions(self, symbol: str) -> list[CorporateAction]:
        rows = await self._get(
            symbol, "/api/corporates-corporateActions", {"index": "equities", "symbol": symbol}
        )
        return [CorporateAction.parse(r) for r in rows or []]

    async def annual_reports(self, symbol: str) -> list[AnnualReportFiling]:
        d = await self._get(symbol, "/api/annual-reports", {"index": "equities", "symbol": symbol})
        rows = d.get("data", []) if isinstance(d, dict) else d or []
        return [AnnualReportFiling.parse(r, symbol) for r in rows]

    async def fetch_bytes(self, url: str, *, cache_ttl: float | None = None) -> bytes:
        """GET an archive file. Filing XBRLs carry the filing id in their URL and never change, so callers may pass a
        long `cache_ttl` (on-disk cache)."""
        resp = await self.nse.http.get(url, headers={"Referer": f"{NSE_BASE}/"}, cache_ttl=cache_ttl)
        if not resp.ok:
            raise NseError(f"HTTP {resp.status} for {url}")
        return resp.content


# --------------------------------------------------------------------------- long histories
HISTORY_MAX_REQUESTS = 40  # NSE returns at most ~70 rows (the latest) per historical-trade request
HISTORY_WINDOW_DAYS = 360  # and refuses (HTTP 404) a range longer than a year


async def walk_history(fetch: Callable[[date, date], Awaitable[list[Any]]], start: date, end: date, *,
                       max_requests: int = HISTORY_MAX_REQUESTS,
                       window_days: int = HISTORY_WINDOW_DAYS) -> tuple[list[Any], bool]:  # fmt: skip
    """Daily rows (anything with a `.day`) from `start` to `end`, oldest first, and whether the walk stopped early.

    NSE answers a date range with only its latest ~70 trading days, so this walks backwards from the end until a
    request reaches the start (or returns nothing new). A request that fails after some rows came back ends the
    walk with `partial=True`; a failure before any row came back is raised.
    """
    bars: dict[date, Any] = {}
    hi, partial = end, False
    for _ in range(max_requests):
        if hi < start:
            break
        lo = max(start, hi - timedelta(days=window_days))
        try:
            got = await fetch(lo, hi)
        except Exception:
            if not bars:
                raise
            partial = True  # keep what came back; callers say the history is shorter
            break
        got = [x for x in got if lo <= x.day <= hi]
        for x in got:
            bars[x.day] = x
        if not got and lo == start:
            break
        earliest = min((x.day for x in got), default=lo)
        # a window answered in full (or empty: e.g. before listing) moves to the previous window
        hi = lo - timedelta(days=1) if earliest <= lo + timedelta(days=4) else earliest - timedelta(days=1)
    return [bars[d] for d in sorted(bars)], partial


# --------------------------------------------------------------------------- listed equities (search)
EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


class ListedEquity(BaseModel):
    symbol: str
    name: str
    series: str
    listed: date | None
    isin: str


def parse_equity_list(text: str) -> list[ListedEquity]:
    import csv
    import io

    rows = csv.reader(io.StringIO(text))
    header = [h.strip().upper() for h in next(rows, [])]
    idx = {k: header.index(k) for k in ("SYMBOL", "NAME OF COMPANY", "SERIES", "DATE OF LISTING", "ISIN NUMBER")
           if k in header}  # fmt: skip
    out = []
    for r in rows:
        if len(r) < len(header):
            continue
        out.append(ListedEquity(symbol=r[idx["SYMBOL"]].strip(), name=r[idx["NAME OF COMPANY"]].strip(),
                                series=r[idx["SERIES"]].strip(),
                                listed=parse_nse_date(r[idx["DATE OF LISTING"]].strip().title()),
                                isin=r[idx["ISIN NUMBER"]].strip()))  # fmt: skip
    return out


def search_equities(equities: list[ListedEquity], query: str, limit: int = 15) -> list[ListedEquity]:
    """Exact symbol first, then symbol prefix, then names containing every word of the query."""
    q = query.strip().upper()
    if not q:
        return []
    words = q.split()

    def rank(e: ListedEquity) -> int | None:
        if e.symbol == q:
            return 0
        if e.symbol.startswith(q):
            return 1
        name = e.name.upper()
        if name.startswith(q):
            return 2
        if all(w in name for w in words):
            return 3
        return None

    scored = [(r, e.symbol, e) for e in equities if (r := rank(e)) is not None]
    return [e for _, _, e in sorted(scored)[:limit]]


def _json_body(resp) -> bool:
    head = resp.content.lstrip()[:1]
    return head in (b"{", b"[")

"""NSE listed-equity data: price history, corporate announcements, results filings, shareholding and corporate
actions. Every call warms the quote page first and sends it as referer (NSE refuses scripted clients otherwise).

Recorded payloads live in tests/fixtures/nse/equity/. Dates in the payloads are NSE's (IST).
"""

from __future__ import annotations

import re
from datetime import date, datetime
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


class ResultFiling(BaseModel):
    symbol: str
    period_from: date | None
    period_to: date | None
    relating_to: str | None
    consolidated: bool
    audited: bool | None
    filed_at: datetime | None
    xbrl: str | None

    @classmethod
    def parse(cls, r: dict[str, Any]) -> ResultFiling:
        aud = (r.get("audited") or "").lower()
        return cls(symbol=r.get("symbol", ""), period_from=parse_nse_date(r.get("fromDate")),
                   period_to=parse_nse_date(r.get("toDate")), relating_to=r.get("relatingTo"),
                   consolidated=(r.get("consolidated") or "").lower() == "consolidated",
                   audited=None if not aud else aud == "audited", filed_at=parse_nse_timestamp(r.get("filingDate")),
                   xbrl=r.get("xbrl") or None)  # fmt: skip


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

    async def _get(self, symbol: str, path: str, params: dict[str, str]) -> Any:
        page = f"{NSE_BASE}/get-quotes/equity?symbol={symbol}"
        if not self.nse._warmed:
            self.nse.http.cookies.clear()
            await self.nse.http.get(page)
            self.nse._warmed = True
        resp = await self.nse.http.get(
            f"{NSE_BASE}{path}", params=params, headers={**API_HEADERS, "Referer": page}
        )
        if not resp.ok:
            raise NseError(f"NSE HTTP {resp.status} for {path} ({symbol})")
        try:
            return resp.json()
        except ValueError as e:
            raise NseError(f"NSE returned a non-JSON page for {path} ({symbol})") from e

    async def history(self, symbol: str, start: date, end: date, series: str = "EQ") -> list[PriceBar]:
        rows = await self._get(symbol, "/api/NextApi/apiClient/GetQuoteApi",
                               {"functionName": "getHistoricalTradeData", "symbol": symbol, "series": series,
                                "fromDate": start.strftime("%d-%m-%Y"), "toDate": end.strftime("%d-%m-%Y")})  # fmt: skip
        return sorted((PriceBar.parse(r) for r in rows or []), key=lambda b: b.day)

    async def announcements(self, symbol: str) -> list[Announcement]:
        rows = await self._get(
            symbol, "/api/corporate-announcements", {"index": "equities", "symbol": symbol}
        )
        return [Announcement.parse(r) for r in rows or []]

    async def results(self, symbol: str, period: str = "Quarterly") -> list[ResultFiling]:
        rows = await self._get(symbol, "/api/corporates-financial-results",
                               {"index": "equities", "symbol": symbol, "period": period})  # fmt: skip
        return [ResultFiling.parse(r) for r in rows or []]

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

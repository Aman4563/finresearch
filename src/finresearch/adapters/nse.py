"""NSE IPO adapter: open/upcoming/past issues and live subscription (bid) data.

Endpoints (verified live 28-Sep-2026, all JSON after a cookie warm-up):
- `/api/ipo-current-issue`                 open IPOs, each with its NSE+BSE "Total" subscription row
- `/api/all-upcoming-issues?category=ipo`  open + upcoming IPOs (no subscription figures)
- `/api/public-past-issues`                every past issue since 2012 (~1,450 rows; EQ, SME, debt, ...)
- `/api/ipo-detail?symbol=..&series=EQ`    the full subscription page for one issue, plus its issue information:
                                           price range, "Bid Lot" + "Minimum Order Quantity" (mainboard) or
                                           "Lot Size" (SME) - the lot the two issue lists above do not carry

NSE's API returns 401/403 (or an HTML block page) without the bot-manager cookies that the website
sets, so the client first GETs the public IPO page and re-warms once when the cookies go stale.

Subscription semantics that matter for a correct report:
- `activeCat` is the COMBINED NSE + BSE book by category ("Updated as on dd-Mon-yyyy HH:MM:SS").
  This is the figure the market quotes.
- `bidDetails` is the NSE-ONLY book. It carries no timestamp of its own; we use the NSE demand graph
  timestamp (`demandGraph.timestamp`), which is published with the same refresh.
- NSE computes "shares offered" (and so "times subscribed") on the LOWER end of the price band:
  a fixed-rupee issue buys more shares at the low price. At the upper band there are fewer shares on
  offer, so the true multiple is higher. Categories keep `shares_offered`/`shares_bid` so callers can
  recompute; `rebase_times()` does the arithmetic.
- Numbers arrive as strings, often in Java scientific notation ("2.32524175E8") or Indian digit
  grouping ("6,62,77,860"). They are parsed with `Decimal`, never float, so totals reconcile exactly.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from finresearch.adapters.http import IST, Fetched, FetchRecord, PoliteClient

NSE_BASE = "https://www.nseindia.com"
WARMUP_URL = f"{NSE_BASE}/market-data/all-upcoming-issues-ipo"
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": WARMUP_URL,
    "X-Requested-With": "XMLHttpRequest",
}


class NseError(RuntimeError):
    """NSE refused the request or returned something that is not the expected JSON."""


# --------------------------------------------------------------------------- parsing helpers


def parse_num(value: Any) -> Decimal | None:
    """Parse NSE numeric strings: "", "-", None, "    99", "3779440.0", "1.176E7", "6,62,77,860"."""
    if value is None:
        return None
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip().replace(",", "")
    if text.lower().startswith("rs."):
        text = text[3:].strip()
    if text in {"", "-", "NA", "N/A", "null"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


_TS_PREFIX = re.compile(r"^\s*(updated\s+)?as\s+on\s+", re.IGNORECASE)


def parse_nse_timestamp(value: str | None) -> datetime | None:
    """'Updated as on 28-Sep-2026 13:54:00' / 'As on 28-Sep-2026 14:40:06 IST' / '28-Sep-2026 13:51:00' -> IST."""
    if not value:
        return None
    text = _TS_PREFIX.sub("", value.strip())
    text = re.sub(r"\s*IST\s*$", "", text, flags=re.IGNORECASE).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse_nse_date(value: str | None) -> date | None:
    """'25-SEP-2026' / '25-Sep-2026' -> date ('-' or empty -> None)."""
    if not value or not value.strip() or value.strip() == "-":
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


_PRICE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def parse_price_band(value: str | None) -> tuple[Decimal | None, Decimal | None]:
    """'Rs.159 to Rs.167' / 'Rs. 258 to Rs. 272 per Equity Share' / '402' -> (low, high)."""
    if not value:
        return None, None
    nums = [parse_num(n) for n in _PRICE.findall(value)]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None, None
    return nums[0], nums[-1] if len(nums) > 1 else nums[0]


def rebase_times(times: Decimal, shares_offered_low: Decimal, shares_offered_high: Decimal) -> Decimal:
    """Convert a lower-band multiple (NSE's convention) to the upper-band multiple.

    times_low = bid / offered_low, so times_high = bid / offered_high = times_low * offered_low / offered_high.
    `shares_offered_high` is the category's share count at the upper price (from the RHP/price-band ad).
    """
    if shares_offered_high == 0:
        raise ValueError("shares_offered_high must be > 0")
    return times * shares_offered_low / shares_offered_high


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return text


# --------------------------------------------------------------------------- models


class CategorySubscription(BaseModel):
    name: str
    code: str | None = Field(description="NSE srNo: '1', '1(a)', '2.1', ...; None for the Total row")
    shares_offered: Decimal | None = None  # lower-price-band share base
    shares_bid: Decimal | None = None
    times: Decimal | None = None

    @property
    def is_total(self) -> bool:
        return self.code is None and self.name.strip().lower() == "total"

    @property
    def is_top_level(self) -> bool:
        """QIB / NII / RII / Employee ... rows (codes like '1', '2', '3'), not sub-buckets like '1(a)' or '2.1'."""
        return self.code is not None and self.code.isdigit()


# field names differ between the two tables (NSE typo'd one of them)
_COMBINED_KEYS = ("noOfShareOffered", "noOfSharesBid", "noOfTotalMeant")
_NSE_ONLY_KEYS = ("noOfSharesOffered", "noOfsharesBid", "noOfTime")


def _parse_category_rows(
    rows: list[dict[str, Any]], keys: tuple[str, str, str]
) -> list[CategorySubscription]:
    offered_k, bid_k, times_k = keys
    out: list[CategorySubscription] = []
    for row in rows:
        code = row.get("srNo")
        if code == "Sr.No." or row.get("category") == "Category":
            continue  # header row embedded in activeCat.dataList
        offered = parse_num(row.get(offered_k))
        out.append(
            CategorySubscription(
                name=_clean_text(row.get("category")),
                code=str(code).strip() if code not in (None, "") else None,
                shares_offered=offered,
                shares_bid=parse_num(row.get(bid_k)),
                # SME tables publish no offered shares and print "0.00": that is unknown, not zero demand
                times=parse_num(row.get(times_k)) if offered else None,
            )
        )
    return out


class SubscriptionSnapshot(BaseModel):
    symbol: str
    as_of: datetime | None = Field(description="IST timestamp published by NSE for this table")
    source: Literal["nse_combined", "nse_only", "bse_sme"]
    categories: list[CategorySubscription]
    total_times: Decimal | None = None
    total_shares_offered: Decimal | None = None
    total_shares_bid: Decimal | None = None

    @classmethod
    def from_rows(
        cls,
        symbol: str,
        rows: list[dict[str, Any]],
        *,
        source: Literal["nse_combined", "nse_only"],
        as_of: datetime | None,
    ) -> SubscriptionSnapshot:
        keys = _COMBINED_KEYS if source == "nse_combined" else _NSE_ONLY_KEYS
        cats = _parse_category_rows(rows, keys)
        total = next((c for c in cats if c.is_total), None)
        return cls(
            symbol=symbol,
            as_of=as_of,
            source=source,
            categories=cats,
            total_times=total.times if total else None,
            total_shares_offered=total.shares_offered if total else None,
            total_shares_bid=total.shares_bid if total else None,
        )

    def category(self, code: str) -> CategorySubscription | None:
        return next((c for c in self.categories if c.code == code), None)

    @property
    def top_level(self) -> list[CategorySubscription]:
        return [c for c in self.categories if c.is_top_level]


class DemandPoint(BaseModel):
    price: str  # "272" or "Cut-Off"
    is_cutoff: bool
    cumulative_shares: Decimal | None


class DemandGraph(BaseModel):
    """Price-wise cumulative demand. `scope='combined'` = demandGraphALL (NSE+BSE, hourly), 'nse' = demandGraph."""

    scope: Literal["combined", "nse"]
    as_of: datetime | None
    times_subscribed: Decimal | None = Field(description="Headline, rounded to 2dp by NSE")
    total_bids: Decimal | None
    bids_at_cutoff: Decimal | None
    total_issue_size: Decimal | None
    points: list[DemandPoint]

    @classmethod
    def parse(cls, data: dict[str, Any], scope: Literal["combined", "nse"]) -> DemandGraph:
        points = [
            DemandPoint(
                price=str(price), is_cutoff=str(price).lower() == "cut-off", cumulative_shares=parse_num(qty)
            )
            for price, qty in (data.get("plotData") or {}).items()
        ]
        return cls(
            scope=scope,
            as_of=parse_nse_timestamp(data.get("timestamp")),
            times_subscribed=parse_num(data.get("noOfTimesIssueSubscribed")),
            total_bids=parse_num(data.get("totalBidRecieved") or data.get("TOTAL_BIDS")),
            bids_at_cutoff=parse_num(data.get("totalBidAtCutOff")),
            total_issue_size=parse_num(data.get("totalIssueSize")),
            points=points,
        )

    @property
    def cutoff(self) -> DemandPoint | None:
        return next((p for p in self.points if p.is_cutoff), None)


class IpoDetail(BaseModel):
    symbol: str
    series: str = "EQ"  # EQ (mainboard) or SME (NSE Emerge)
    company_name: str | None = None
    combined: SubscriptionSnapshot | None = Field(default=None, description="activeCat: NSE+BSE")
    nse_only: SubscriptionSnapshot | None = Field(default=None, description="bidDetails: NSE only")
    demand_combined: DemandGraph | None = None
    demand_nse: DemandGraph | None = None
    issue_info: dict[str, str] = Field(default_factory=dict)
    fetch: FetchRecord | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def price_band(self) -> tuple[Decimal | None, Decimal | None]:
        return parse_price_band(self.issue_info.get("Price Range"))

    @property
    def terms(self) -> IssueTerms:
        return parse_issue_terms(self.symbol, self.issue_info, self.series, self.fetch)


# ICDR (Mar-2025): SME individual investors bid exactly 2 lots; NSE's SME page gives no minimum
SME_MIN_LOTS = 2
SME_MIN_BASIS = "SME minimum application: 2 lots (SEBI ICDR 2025; NSE Security Parameters)"
TERMS_TTL_S = 6 * 3600  # a lot size does not change once the price band is out
_LOT_KEYS = ("Bid Lot", "Lot Size", "Market Lot")
_MIN_KEYS = ("Minimum Order Quantity", "Minimum Bid Quantity", "Minimum Bid", "Minimum Order Size")
_LOT_IN_BODY = re.compile(rb'"(Bid Lot|Lot Size|Market Lot)\s*"')
_FIRST_INT = re.compile(r"\d[\d,]*")


def _first_int(value: str | None) -> int | None:
    """'1600 Equity Shares and in multiples thereof' -> 1600; '"Rs. 2,00,000"' -> 200000."""
    m = _FIRST_INT.search(value or "")
    return int(m.group().replace(",", "")) if m else None


def _info(info: dict[str, str], keys: tuple[str, ...]) -> str | None:
    norm = {k.strip().lower(): v for k, v in info.items()}
    return next((norm[k.lower()] for k in keys if k.lower() in norm), None)


class IssueTerms(BaseModel):
    """Lot, minimum bid and price band of one issue, from NSE's issue information (or BSE's issue details)."""

    symbol: str
    series: str = "EQ"
    lot_size: int | None = None
    min_bid_shares: int | None = None
    min_lots: int | None = None
    min_lots_basis: str | None = None
    price_range: str | None = None
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    retail_cap: Decimal | None = None  # "Maximum Subscription Amount for Retail Investor"
    source: str = "NSE issue information"
    source_url: str | None = None
    as_of: datetime | None = None

    @property
    def is_sme(self) -> bool:
        return self.series.upper() == "SME"


def parse_issue_terms(symbol: str, info: dict[str, str], series: str = "EQ",
                      fetch: FetchRecord | None = None) -> IssueTerms:  # fmt: skip
    """issue_info (NSE titles) -> IssueTerms. Mainboard pages carry "Bid Lot" and "Minimum Order Quantity"; SME
    pages carry only "Lot Size", and SME individual investors must bid 2 lots, so the minimum is that rule."""
    lot = _first_int(_info(info, _LOT_KEYS))
    min_bid = _first_int(_info(info, _MIN_KEYS))
    sme = series.upper() == "SME"
    min_lots, basis = None, None
    if lot and min_bid and min_bid % lot == 0:
        min_lots, basis = min_bid // lot, "NSE Minimum Order Quantity"
    elif lot and sme:
        min_lots, basis, min_bid = SME_MIN_LOTS, SME_MIN_BASIS, lot * SME_MIN_LOTS
    elif lot:
        min_lots, basis, min_bid = 1, "one lot (NSE publishes no separate minimum)", lot
    price_range = _info(info, ("Price Range", "Issue Price"))
    low, high = parse_price_band(price_range)
    cap = _first_int(_info(info, ("Maximum Subscription Amount for Retail Investor",)))
    return IssueTerms(symbol=symbol, series=series, lot_size=lot, min_bid_shares=min_bid, min_lots=min_lots,
                      min_lots_basis=basis, price_range=price_range, price_low=low, price_high=high,
                      retail_cap=Decimal(cap) if cap else None,
                      source_url=fetch.url if fetch else None, as_of=fetch.fetched_at if fetch else None)  # fmt: skip


class IpoIssue(BaseModel):
    """A row of ipo-current-issue / all-upcoming-issues."""

    symbol: str
    company: str
    series: str | None = None
    issue_start: date | None = None
    issue_end: date | None = None
    price_band: str | None = None
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    issue_size_shares: Decimal | None = None
    status: str | None = None
    # present only in ipo-current-issue (its embedded "Total" row, NSE+BSE)
    shares_offered: Decimal | None = None
    shares_bid: Decimal | None = None
    times_subscribed: Decimal | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, row: dict[str, Any]) -> IpoIssue:
        low, high = parse_price_band(row.get("issuePrice"))
        return cls(
            symbol=str(row.get("symbol", "")).strip(),
            company=_clean_text(row.get("companyName") or row.get("company")),
            series=row.get("series"),
            issue_start=parse_nse_date(row.get("issueStartDate")),
            issue_end=parse_nse_date(row.get("issueEndDate")),
            price_band=row.get("issuePrice"),
            price_low=low,
            price_high=high,
            issue_size_shares=parse_num(row.get("issueSize")),
            status=row.get("status"),
            shares_offered=parse_num(row.get("noOfSharesOffered")),
            shares_bid=parse_num(row.get("noOfsharesBid")),
            times_subscribed=parse_num(row.get("noOfTime")),
            raw=row,
        )


class PastIssue(BaseModel):
    """A row of public-past-issues. Rows are heterogeneous (company vs companyName, '-' placeholders)."""

    symbol: str
    company: str
    security_type: str | None = None  # EQ, SME, BE, DEBT, ...
    ipo_start: date | None = None
    ipo_end: date | None = None
    listing_date: date | None = None
    issue_price: Decimal | None = None
    price_range: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, row: dict[str, Any]) -> PastIssue:
        return cls(
            symbol=str(row.get("symbol", "")).strip(),
            company=_clean_text(row.get("company") or row.get("companyName")),
            security_type=row.get("securityType"),
            ipo_start=parse_nse_date(row.get("ipoStartDate")),
            ipo_end=parse_nse_date(row.get("ipoEndDate")),
            listing_date=parse_nse_date(row.get("listingDate")),
            issue_price=parse_num(row.get("issuePrice")),
            price_range=row.get("priceRange"),
            raw=row,
        )


def parse_issue_info(data: dict[str, Any] | None) -> dict[str, str]:
    """issueInfo.dataList title/value pairs -> dict. Drops the untitled SEBI-circular blurb and the name row."""
    out: dict[str, str] = {}
    for item in (data or {}).get("dataList") or []:
        title = _clean_text(item.get("title"))
        value = _clean_text(item.get("value"))
        if not title or not value:
            continue
        out[title] = value
    return out


def parse_ipo_detail(
    symbol: str, data: dict[str, Any], fetch: FetchRecord | None = None, series: str = "EQ"
) -> IpoDetail:
    """Parse an /api/ipo-detail payload. Missing sections (closed/unknown issue) become None, not errors."""
    active = data.get("activeCat") or {}
    combined = None
    if active.get("dataList"):
        combined = SubscriptionSnapshot.from_rows(
            symbol,
            active["dataList"],
            source="nse_combined",
            as_of=parse_nse_timestamp(active.get("updateTime")),
        )
    nse_graph = data.get("demandGraph") or {}
    nse_only = None
    if data.get("bidDetails"):
        nse_only = SubscriptionSnapshot.from_rows(
            symbol,
            data["bidDetails"],
            source="nse_only",
            as_of=parse_nse_timestamp(nse_graph.get("timestamp")),
        )
    info_rows = (data.get("issueInfo") or {}).get("dataList") or []
    company = _clean_text(info_rows[0].get("title")) if info_rows else None
    return IpoDetail(
        symbol=symbol,
        series=series,
        company_name=company or None,
        combined=combined,
        nse_only=nse_only,
        demand_combined=DemandGraph.parse(data["demandGraphALL"], "combined")
        if data.get("demandGraphALL")
        else None,
        demand_nse=DemandGraph.parse(nse_graph, "nse") if nse_graph else None,
        issue_info=parse_issue_info(data.get("issueInfo")),
        fetch=fetch,
        raw=data,
    )


# --------------------------------------------------------------------------- client


class Quote(BaseModel):
    """An equity quote from NSE's quote API (lastUpdateTime is NSE's IST timestamp)."""

    symbol: str
    company: str | None = None
    open: Decimal | None = None
    last_price: Decimal | None = None
    close_price: Decimal | None = None
    previous_close: Decimal | None = None
    listing_date: date | None = None
    status: str | None = None
    as_of: datetime | None = None
    week52_high: Decimal | None = None
    week52_low: Decimal | None = None
    issued_shares: Decimal | None = None
    industry: str | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Quote:
        rows = data.get("equityResponse") or []
        if not rows:
            raise NseError("quote payload has no equityResponse")
        e = rows[0]
        meta, trade, sec = e.get("metaData") or {}, e.get("tradeInfo") or {}, e.get("secInfo") or {}
        price = e.get("priceInfo") or {}
        listing = str(sec.get("listingDate") or "").split(" ")[0]
        return cls(symbol=str(meta.get("symbol", "")).strip(), company=meta.get("companyName"),
                   open=parse_num(meta.get("open")), last_price=parse_num(trade.get("lastPrice")),
                   close_price=parse_num(meta.get("closePrice")) or None,
                   previous_close=parse_num(meta.get("previousClose")), listing_date=parse_nse_date(listing),
                   status=sec.get("secStatus"), as_of=parse_nse_timestamp(e.get("lastUpdateTime")),
                   week52_high=parse_num(price.get("yearHigh")), week52_low=parse_num(price.get("yearLow")),
                   issued_shares=parse_num(trade.get("issuedSize")),
                   industry=(sec.get("basicIndustry") or None))  # fmt: skip


class NseClient:
    """NSE IPO endpoints with cookie warm-up. Owns its PoliteClient unless one is passed in."""

    def __init__(self, client: PoliteClient | None = None, *, warmup_url: str = WARMUP_URL) -> None:
        self._own = client is None
        self.http = client or PoliteClient()
        self.warmup_url = warmup_url
        self._warmed = False

    async def __aenter__(self) -> NseClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own:
            await self.http.aclose()

    async def warm_up(self) -> None:
        self.http.cookies.clear()
        resp = await self.http.get(self.warmup_url)
        if not resp.ok:
            raise NseError(f"NSE warm-up failed: HTTP {resp.status} for {self.warmup_url}")
        self._warmed = True

    async def get_json(self, path: str, params: dict[str, str] | None = None, *, cache_ttl: float | None = None,
                       cache_if: Callable[[Fetched], bool] | None = None) -> tuple[Any, Fetched]:  # fmt: skip
        """GET an /api path. Re-warms cookies once on 401/403 or a non-JSON (block page) response.

        With `cache_ttl`, only JSON bodies (that also pass `cache_if`) are cached: a 200 block page never is."""
        url = path if path.startswith("http") else f"{NSE_BASE}{path}"
        if not self._warmed:
            await self.warm_up()
        for attempt in range(2):
            vet = (
                (lambda r: _looks_json(r) and (cache_if is None or cache_if(r)))
                if cache_ttl is not None
                else None
            )
            resp = await self.http.get(
                url, params=params, headers=API_HEADERS, cache_ttl=cache_ttl, cache_if=vet
            )
            if resp.status in (401, 403) or (resp.ok and not _looks_json(resp)):
                if attempt == 0:
                    await self.warm_up()
                    continue
                raise NseError(f"NSE refused {url} after re-warm: HTTP {resp.status}")
            if not resp.ok:
                raise NseError(f"NSE HTTP {resp.status} for {url}")
            try:
                return resp.json(), resp
            except ValueError as exc:
                raise NseError(f"NSE returned invalid JSON for {url}") from exc
        raise NseError(f"NSE refused {url}")  # pragma: no cover - loop always returns/raises

    async def current_issues(self) -> list[IpoIssue]:
        data, _ = await self.get_json("/api/ipo-current-issue")
        return [IpoIssue.parse(r) for r in data or []]

    async def upcoming_issues(self, category: str = "ipo") -> list[IpoIssue]:
        data, _ = await self.get_json("/api/all-upcoming-issues", {"category": category})
        return [IpoIssue.parse(r) for r in data or []]

    async def past_issues(self) -> list[PastIssue]:
        data, _ = await self.get_json("/api/public-past-issues")
        return [PastIssue.parse(r) for r in data or []]

    async def warm_quote_session(self, symbol: str) -> None:
        """Fresh cookies from a quote page (what `quote` does before every call unless `warm=False`)."""
        self.http.cookies.clear()
        await self.http.get(f"{NSE_BASE}/get-quotes/equity?symbol={symbol}")
        self._warmed = True

    async def quote(self, symbol: str, series: str = "EQ", *, warm: bool = True) -> Quote:
        """Equity quote (open, last price, listing date). The classic quote-equity API refuses scripted clients,
        so this uses the quote page's own API with the quote page as referer. `warm=False` reuses the session's
        cookies (a batch of quotes warms once: portfolio.valuation.QuoteBatch)."""
        page = f"{NSE_BASE}/get-quotes/equity?symbol={symbol}"
        if warm:
            await self.warm_quote_session(symbol)
        resp = await self.http.get(f"{NSE_BASE}/api/NextApi/apiClient/GetQuoteApi",
                                   params={"functionName": "getSymbolData", "marketType": "N", "series": series,
                                           "symbol": symbol}, headers={**API_HEADERS, "Referer": page})  # fmt: skip
        if not resp.ok or not _looks_json(resp):
            raise NseError(f"NSE quote refused for {symbol}: HTTP {resp.status}")
        return Quote.parse(resp.json())

    async def ipo_detail(self, symbol: str, series: str | None = None) -> IpoDetail:
        """Issue detail. Without a series, mainboard (EQ) is tried first and SME second: NSE answers an SME symbol
        queried as EQ with an empty issue-information table."""
        for ser in [series] if series else ["EQ", "SME"]:
            data, resp = await self.get_json("/api/ipo-detail", {"symbol": symbol, "series": ser})
            if not isinstance(data, dict):
                raise NseError(f"Unexpected ipo-detail payload for {symbol}: {type(data).__name__}")
            detail = parse_ipo_detail(symbol, data, fetch=resp.record, series=ser)
            if detail.issue_info or series:
                return detail
        return detail

    async def issue_terms(self, symbol: str, series: str | None = None) -> IssueTerms:
        """Lot, minimum bid and price band from the issue's detail page. Cached for hours, but only once the page
        carries a lot: an upcoming issue whose lot NSE has not published yet is re-read on the next call."""
        ser = (series or "EQ").upper()
        ser = ser if ser in ("EQ", "SME") else "EQ"
        data, resp = await self.get_json("/api/ipo-detail", {"symbol": symbol, "series": ser}, cache_ttl=TERMS_TTL_S,
                                         cache_if=lambda r: bool(_LOT_IN_BODY.search(r.content)))  # fmt: skip
        if not isinstance(data, dict):
            raise NseError(f"Unexpected ipo-detail payload for {symbol}: {type(data).__name__}")
        return parse_issue_terms(symbol, parse_issue_info(data.get("issueInfo")), ser, resp.record)


def _looks_json(resp: Fetched) -> bool:
    ctype = (resp.record.content_type or "").lower()
    if "json" in ctype:
        return True
    head = resp.content.lstrip()[:1]
    return head in (b"{", b"[")

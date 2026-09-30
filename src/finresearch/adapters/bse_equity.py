"""BSE listed-equity data: scrip master, quote, daily price history, the equity bhavcopy, announcements, corporate
actions, results (integrated-filing XBRL) and shareholding (SHP XBRL), for BSE-only and dual-listed stocks.

Endpoints (api.bseindia.com/BseIndiaAPI/api, verified live 30-Sep-2026 with `bse.BSE_HEADERS`; no cookies needed.
The endpoint names come from BSE's own site bundle, which calls them the same way):
- `/ListofScripData/w?segment=Equity&status=Active`       scrip master: code, scrip id, name, ISIN, group, face value,
                                                           market cap (Rs crore); ~5,000 rows, 1.7 MB
- `/getScripHeaderData/w?scripcode=N`                     LTP, open, previous close, `Ason` (parser in adapters/bse.py)
- `/ComHeadernew/w?quotetype=EQ&scripcode=N`              security id, ISIN, group, face value, industry, sector
- `/StockTrading/w?quotetype=EQ&scripcode=N`              full market cap in Rs crore ("4,09,074.28")
- `/HighLow/w?Type=EQ&flag=C&scripcode=N`                 52-week high/low (adjusted for corporate actions)
- `/StockPriceCSVDownload/w?pageType=0&rbType=D&Scode=N&FDates=dd/mm/yyyy&TDates=dd/mm/yyyy`
                                                           daily OHLC, WAP, volume, trades, turnover for any range in
                                                           one CSV (10 years = 2,478 rows, no cap seen)
- `/AnnSubCategoryGetData/w?strScrip=N&strPrevDate=YYYYMMDD&strToDate=YYYYMMDD&...`  announcements; PDFs under
                                                           www.bseindia.com/xml-data/corpfiling/AttachLive/
- `/DefaultData/w?scripcode=N&ddlcategorys=E&segment=0&strSearch=S`  every corporate action (ex-date, purpose,
                                                           record date). `/CorporateAction/w` keeps only the last five.
- `/Integratedfinancedata/w?scripcode=N`                  Integrated Filing (Financials) index from the Mar-2025
                                                           quarter; each row's iXBRL `.html` has its XBRL instance at
                                                           the same path with `.xml` (in-capmkt taxonomy, parsed by
                                                           adapters/xbrl.py). Older quarters are not indexed here.
- `/SHPQNewFormat/w?scripcode=N`                          every filed shareholding pattern with its XBRL file name,
                                                           served at www.bseindia.com/XBRLFILES/SHPXBRLDataXML/<file>
                                                           (in-bse-shp taxonomy, parsed by adapters/shp_xbrl.py)
- `/CorporatesSHPSecuritybeta/w?scripcode=N&qtrid=`       the latest pattern's summary: promoter / public / DR /
                                                           employee trusts (% of A+B+C2)
- Equity bhavcopy (UDiFF, since BSE's 2024 change): www.bseindia.com/download/BhavCopy/Equity/
  BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV (~860 KB, every CM instrument). The old EQ_ISINCODE_DDMMYY.zip URL
  answers HTTP 200 with the site's HTML shell, so a body that is not the CSV header counts as "not published".

NSE and BSE share the ISIN, which is how a scrip is matched across exchanges (`merge_listings`). A BSE scrip id
can equal an unrelated NSE symbol (National Stock Exchange of India Ltd trades on BSE as "NSE", code 544937), so
BSE-only stocks are keyed by scrip code ("BSE:544937"), never by scrip id.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from finresearch.adapters.bse import BSE_API, BSE_HEADERS, BseClient, BseError, parse_scrip_header
from finresearch.adapters.http import IST
from finresearch.adapters.nse import Quote, parse_num
from finresearch.adapters.nse_equity import (
    INTEGRATED_FINANCIALS,
    Announcement,
    CorporateAction,
    IntegratedFiling,
    ListedEquity,
    PriceBar,
    ResultFiling,
    Shareholding,
    parse_results_period_end,
)

BSE_WWW = "https://www.bseindia.com"
BHAVCOPY_URL = BSE_WWW + "/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{day:%Y%m%d}_F_0000.CSV"
ATTACHMENT_URL = BSE_WWW + "/xml-data/corpfiling/AttachLive/{name}"
SHP_XBRL_URL = BSE_WWW + "/XBRLFILES/SHPXBRLDataXML/{name}"
BSE_FILE_HOSTS = ("www.bseindia.com",)
SCRIP_LIST_TTL_S = 86400  # the scrip master changes a few times a day at most
BHAVCOPY_TTL_S = 30 * 86400  # a published day's file never changes
ANNOUNCEMENT_DAYS = 120
SCRIP_KEY = re.compile(r"^BSE:(\d{6})$")


def bse_key(code: str) -> str:
    return f"BSE:{code}"


def scrip_code_of(key: str) -> str | None:
    """'BSE:500209' -> '500209'; anything else -> None."""
    m = SCRIP_KEY.match((key or "").strip().upper())
    return m.group(1) if m else None


def official_file(url: str | None) -> bool:
    """A BSE filing file (XBRL, iXBRL, attachment) over https on www.bseindia.com."""
    if not url:
        return False
    p = urlsplit(url)
    return p.scheme == "https" and (p.hostname or "") in BSE_FILE_HOSTS and p.port in (None, 443) and \
        (p.path.startswith("/XBRLFILES/") or p.path.startswith("/xml-data/"))  # fmt: skip


# --------------------------------------------------------------------------- models and parsers
class BseScrip(BaseModel):
    """A row of BSE's scrip master (active equity)."""

    code: str
    symbol: str  # BSE's scrip id, e.g. INFY; not unique across exchanges
    name: str
    isin: str
    group: str | None = None  # A, B, T, X, XT, M/MT (SME), Z (non-compliant), ...
    face_value: Decimal | None = None
    market_cap_cr: Decimal | None = None
    url: str | None = None

    @property
    def is_sme(self) -> bool:
        return (self.group or "").upper() in {"M", "MT", "MS"}


def parse_scrip_list(rows: list[dict[str, Any]]) -> list[BseScrip]:
    out = []
    for r in rows or []:
        code, isin = str(r.get("SCRIP_CD") or "").strip(), str(r.get("ISIN_NUMBER") or "").strip().upper()
        if not code.isdigit() or not isin or (r.get("Segment") or "Equity") != "Equity":
            continue
        out.append(BseScrip(code=code, symbol=str(r.get("scrip_id") or "").strip().upper(),
                            name=str(r.get("Scrip_Name") or r.get("Issuer_Name") or "").strip(), isin=isin,
                            group=(str(r.get("GROUP") or "").strip() or None), face_value=parse_num(r.get("FACE_VALUE")),
                            market_cap_cr=parse_num(r.get("Mktcap")), url=r.get("NSURL") or None))  # fmt: skip
    return out


class BseQuote(Quote):
    """A BSE quote: the NSE quote's fields plus what BSE gives directly (market cap, ISIN, group)."""

    exchange: str = "BSE"
    scrip_code: str | None = None
    isin: str | None = None
    group: str | None = None
    face_value: Decimal | None = None
    market_cap: Decimal | None = None  # rupees (BSE publishes Rs crore)
    free_float_market_cap: Decimal | None = None
    page_url: str | None = None


def build_quote(code: str, header: dict[str, Any], info: dict[str, Any] | None, trading: dict[str, Any] | None,
                highlow: dict[str, Any] | None) -> BseQuote | None:  # fmt: skip
    """getScripHeaderData + ComHeadernew + StockTrading + HighLow -> BseQuote (None: BSE has no quote for the code)."""
    base = parse_scrip_header(code, header or {})
    if base is None:
        return None
    info, trading, highlow = info or {}, trading or {}, highlow or {}
    crore = Decimal(10_000_000)
    mcap, ff = parse_num(trading.get("MktCapFull")), parse_num(trading.get("MktCapFF"))
    seo = ((header or {}).get("Cmpname") or {}).get("SEOUrlEQ")
    fields = base.model_dump()
    fields.update(symbol=str(info.get("SecurityId") or code).strip().upper(), scrip_code=code,
                  isin=(info.get("ISIN") or None), group=(info.get("Group") or None),
                  face_value=parse_num(info.get("FaceVal")),
                  industry=(info.get("Industry") or info.get("IndustryNew") or None),
                  week52_high=parse_num(highlow.get("Fifty2WkHigh_adj")),
                  week52_low=parse_num(highlow.get("Fifty2WkLow_adj")),
                  market_cap=mcap * crore if mcap is not None else None,
                  free_float_market_cap=ff * crore if ff is not None else None,
                  page_url=BSE_WWW + seo if seo else None)  # fmt: skip
    return BseQuote(**fields)


def parse_price_csv(text: str) -> list[PriceBar]:
    """StockPriceCSVDownload -> PriceBars, oldest first; the previous close is the previous row's close."""
    rows = list(csv.reader(io.StringIO((text or "").lstrip("﻿"))))
    if not rows or not rows[0] or rows[0][0].strip() != "Date":
        raise BseError("BSE price history is not the expected CSV")
    head = [h.strip() for h in rows[0]]
    col = {name: head.index(name) for name in head}

    def num(r: list[str], name: str) -> Decimal | None:
        i = col.get(name)
        return parse_num(r[i]) if i is not None and i < len(r) else None

    bars = []
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        try:
            day = datetime.strptime(r[0].strip(), "%d-%B-%Y").date()
        except ValueError:
            continue
        bars.append(PriceBar(day=day, open=num(r, "Open Price"), high=num(r, "High Price"), low=num(r, "Low Price"),
                             close=num(r, "Close Price"), prev_close=None, vwap=num(r, "WAP"),
                             volume=num(r, "No.of Shares"), value_inr=num(r, "Total Turnover (Rs.)"),
                             trades=num(r, "No. of Trades")))  # fmt: skip
    bars.sort(key=lambda b: b.day)
    for prev, cur in pairwise(bars):
        cur.prev_close = prev.close
    return bars


class BhavRow(BaseModel):
    """One equity row of BSE's UDiFF bhavcopy."""

    day: date
    code: str
    isin: str
    symbol: str
    group: str | None
    name: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    last: Decimal | None
    prev_close: Decimal | None
    volume: Decimal | None
    value_inr: Decimal | None
    trades: Decimal | None


def parse_bhavcopy(text: str) -> list[BhavRow]:
    """The UDiFF CM bhavcopy (TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,...) -> rows."""
    reader = csv.DictReader(io.StringIO((text or "").lstrip("﻿")))
    if not reader.fieldnames or "FinInstrmId" not in reader.fieldnames:
        raise BseError("not a BSE UDiFF bhavcopy")
    out = []
    for r in reader:
        try:
            day = date.fromisoformat((r.get("TradDt") or "").strip())
        except ValueError:
            continue
        out.append(BhavRow(day=day, code=(r.get("FinInstrmId") or "").strip(), isin=(r.get("ISIN") or "").strip(),
                           symbol=(r.get("TckrSymb") or "").strip(), group=(r.get("SctySrs") or "").strip() or None,
                           name=(r.get("FinInstrmNm") or "").strip(), open=parse_num(r.get("OpnPric")),
                           high=parse_num(r.get("HghPric")), low=parse_num(r.get("LwPric")),
                           close=parse_num(r.get("ClsPric")), last=parse_num(r.get("LastPric")),
                           prev_close=parse_num(r.get("PrvsClsgPric")), volume=parse_num(r.get("TtlTradgVol")),
                           value_inr=parse_num(r.get("TtlTrfVal")), trades=parse_num(r.get("TtlNbOfTxsExctd"))))  # fmt: skip
    return out


def _bse_datetime(v: Any) -> datetime | None:
    """'2026-09-18T10:31:27.37' (IST) -> aware datetime."""
    text = str(v or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19]).replace(tzinfo=IST)
    except ValueError:
        return None


def _day(v: Any, fmt: str = "%d %b %Y") -> date | None:
    try:
        return datetime.strptime(str(v or "").strip(), fmt).date()
    except ValueError:
        return None


def parse_announcements(code: str, data: dict[str, Any]) -> list[Announcement]:
    out = []
    for r in (data or {}).get("Table") or []:
        text = str(r.get("HEADLINE") or "").strip() or str(r.get("NEWSSUB") or "").strip()
        subject = str(r.get("NEWSSUB") or "").strip()
        name = str(r.get("ATTACHMENTNAME") or "").strip()
        attachment = ATTACHMENT_URL.format(name=name) if name and re.fullmatch(r"[\w.\-]+", name) else None
        out.append(Announcement(symbol=code, at=_bse_datetime(r.get("NEWS_DT") or r.get("DT_TM")),
                                category=str(r.get("CATEGORYNAME") or r.get("SUBCATNAME") or "").strip(),
                                text=text if not subject or subject in text else f"{subject}: {text}",
                                attachment=attachment,
                                results_period_end=parse_results_period_end(f"{subject} {text}")))  # fmt: skip
    return out


_BSE_DIVIDEND = re.compile(r"dividend\s*-\s*rs\.?\s*-\s*(\d+(?:\.\d+)?)", re.I)


def bse_dividend_per_share(purpose: str) -> Decimal | None:
    """'Interim Dividend - Rs. - 6.0000' -> 6 (summed when a purpose lists several); None when not a dividend."""
    amounts = _BSE_DIVIDEND.findall(purpose or "")
    total = sum((Decimal(a) for a in amounts), Decimal(0)) if amounts else None
    # BSE writes 6.0000: keep the value, drop the padding
    return total.normalize() if total is not None else None


def parse_corporate_actions(code: str, rows: list[dict[str, Any]]) -> list[CorporateAction]:
    out = []
    for r in rows or []:
        purpose = str(r.get("Purpose") or "").strip()
        out.append(CorporateAction(symbol=code, subject=purpose, ex_date=_day(r.get("Ex_date")),
                                   record_date=_day(r.get("RD_Date")), dividend_per_share=bse_dividend_per_share(purpose)))  # fmt: skip
    return out


_PERIOD_NAME = re.compile(r"^(Consolidated|Standalone)-([A-Za-z]{3})-(Qtr|Hly|NineMths|Yearly)-(\d{4})$")


def _month_end(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def parse_integrated_financials(code: str, data: dict[str, Any]) -> list[IntegratedFiling]:
    """Integratedfinancedata -> one filing per XBRL file. BSE lists a filing once per view ("Consolidated-Dec-Qtr-2025"
    and "Consolidated-Dec-NineMths-2025" share one file); a company that files half-yearly figures in March appears
    only as "Mar-Hly"/"Mar-Yearly", and its XBRL's current period is then six months long, which the results route
    checks. Newest first, as BSE lists them."""
    out: dict[str, IntegratedFiling] = {}
    for r in (data or {}).get("Table") or []:
        m = _PERIOD_NAME.match(str(r.get("Quarter_Name") or "").strip())
        path = str(r.get("xbrlurl") or r.get("XbrlFile") or "").strip()
        if not m or not path.startswith("/XBRLFILES/") or not path.endswith(".html"):
            continue
        try:
            end = _month_end(int(m.group(4)), datetime.strptime(m.group(2), "%b").month)
        except ValueError:
            continue
        xbrl = BSE_WWW + path[: -len(".html")] + ".xml"
        if xbrl in out and m.group(3) != "Qtr":
            continue
        status = str(r.get("status") or "").strip() or None
        out[xbrl] = IntegratedFiling(symbol=code, kind=INTEGRATED_FINANCIALS, period_end=end,
                                     consolidated=m.group(1) == "Consolidated", audited=None,
                                     filed_at=_bse_datetime(r.get("filing_date_time")),
                                     revised_at=_bse_datetime(r.get("revised_date_time")), sub_type=status,
                                     xbrl=xbrl, ixbrl=BSE_WWW + path, pdf=None, source="bse_integrated_filing")  # fmt: skip
    return list(out.values())


def _shp_quarter(label: str) -> date | None:
    """'June 2026' -> 2026-06-30 (a quarter-end pattern); '23 Sep 2026' -> that day (an event filing)."""
    text = (label or "").strip()
    for fmt in ("%B %Y", "%b %Y", "%b-%y"):
        try:
            d = datetime.strptime(text, fmt).date()
            return _month_end(d.year, d.month)
        except ValueError:
            continue
    return _day(text)


def parse_shareholding_index(code: str, data: dict[str, Any]) -> list[Shareholding]:
    """SHPQNewFormat -> one row per filed pattern with its XBRL link (percentages come from the XBRL or the
    summary; this index has none). Newest first."""
    out = []
    for r in (data or {}).get("Table") or []:
        name = str(r.get("XbrlFile") or "").strip()
        as_of = _shp_quarter(str(r.get("qtr") or ""))
        stamp = _bse_datetime(r.get("revised_date_time")) or _bse_datetime(r.get("filing_date_time"))
        out.append(Shareholding(symbol=code, as_of=as_of, promoter_pct=None, public_pct=None, employee_trusts_pct=None,
                                submitted=stamp.date() if stamp else None,
                                xbrl=SHP_XBRL_URL.format(name=name) if re.fullmatch(r"[\w.\-]+\.xml", name) else None))  # fmt: skip
    return sorted(out, key=lambda x: x.as_of or date.min, reverse=True)


def parse_shareholding_summary(data: dict[str, Any]) -> tuple[date | None, dict[str, Decimal | None]]:
    """CorporatesSHPSecuritybeta -> (quarter end, {promoter, public, employee_trusts} % of A+B+C2)."""
    data = data or {}
    info = next(iter(data.get("Table3") or []), {})
    end = _day(info.get("fld_enddate"))
    pct: dict[str, Decimal | None] = {"promoter": None, "public": None, "employee_trusts": None}
    for r in data.get("Table1") or []:
        key = {"STA1A2": "promoter", "STB1B2B3": "public", "STC2": "employee_trusts"}.get(
            str(r.get("Fld_Code"))
        )
        if key:
            pct[key] = parse_num(r.get("Fld_TotalPercentageOf_A_B_C2"))
    return end, pct


# --------------------------------------------------------------------------- listings across exchanges
class Listing(BaseModel):
    """One company's equity across NSE and BSE, matched by ISIN. `key` is what the stock routes and pages use:
    the NSE symbol when the stock trades on NSE (the default exchange), else "BSE:<scrip code>"."""

    key: str
    symbol: str  # NSE symbol, else BSE scrip id
    name: str
    isin: str
    exchange: str  # "NSE" | "BSE" | "both"
    exchanges: list[str]
    nse_symbol: str | None = None
    series: str | None = None
    listed: date | None = None
    bse_code: str | None = None
    bse_symbol: str | None = None
    bse_group: str | None = None
    bse_url: str | None = None
    market_cap_cr: Decimal | None = None


class Listings(BaseModel):
    """Merged NSE + BSE listings with lookups by key, NSE symbol, BSE code and ISIN."""

    rows: list[Listing] = Field(default_factory=list)

    def model_post_init(self, _ctx: Any) -> None:
        self._by_nse = {r.nse_symbol: r for r in self.rows if r.nse_symbol}
        self._by_code = {r.bse_code: r for r in self.rows if r.bse_code}

    def by_nse(self, symbol: str) -> Listing | None:
        return self._by_nse.get(symbol.upper())

    def by_code(self, code: str) -> Listing | None:
        return self._by_code.get(code)


def merge_listings(nse: list[ListedEquity], bse: list[BseScrip]) -> Listings:
    by_isin = {s.isin: s for s in bse}
    rows, seen = [], set()
    for e in nse:
        b = by_isin.get(e.isin.upper())
        seen.add(e.isin.upper())
        rows.append(Listing(key=e.symbol, symbol=e.symbol, name=e.name, isin=e.isin, exchange="both" if b else "NSE",
                            exchanges=["NSE", "BSE"] if b else ["NSE"], nse_symbol=e.symbol, series=e.series,
                            listed=e.listed, bse_code=b.code if b else None, bse_symbol=b.symbol if b else None,
                            bse_group=b.group if b else None, bse_url=b.url if b else None,
                            market_cap_cr=b.market_cap_cr if b else None))  # fmt: skip
    for b in bse:
        if b.isin in seen:
            continue
        rows.append(Listing(key=bse_key(b.code), symbol=b.symbol or b.code, name=b.name, isin=b.isin, exchange="BSE",
                            exchanges=["BSE"], bse_code=b.code, bse_symbol=b.symbol, bse_group=b.group, bse_url=b.url,
                            market_cap_cr=b.market_cap_cr))  # fmt: skip
    return Listings(rows=rows)


def search_listings(listings: Listings, query: str, limit: int = 15) -> list[Listing]:
    """Exact symbol / BSE code first, then symbol prefix, then name prefix, then names containing every word. Ties:
    listed on both, then larger market cap."""
    q = query.strip().upper()
    if not q:
        return []
    words = q.split()
    code = scrip_code_of(q) or (q if q.isdigit() and len(q) == 6 else None)

    def rank(r: Listing) -> int | None:
        syms = {s for s in (r.nse_symbol, r.bse_symbol) if s}
        if q in syms or (code and r.bse_code == code):
            return 0
        if any(s.startswith(q) for s in syms):
            return 1
        name = r.name.upper()
        if name.startswith(q):
            return 2
        if all(w in name for w in words):
            return 3
        return None

    scored = [(k, len(r.exchanges) == 1, -(r.market_cap_cr or 0), r.symbol, r) for r in listings.rows
              if (k := rank(r)) is not None]  # fmt: skip
    return [s[-1] for s in sorted(scored, key=lambda s: s[:4])[:limit]]


# --------------------------------------------------------------------------- client
class BseEquity:
    """Listed-equity endpoints on top of a BseClient, with NseEquity's method names (the symbol argument is the BSE
    scrip code), so the market routes read either exchange the same way."""

    exchange = "BSE"
    history_window_days = 3660  # one request answers any range...
    answers_full_range = True  # ...in full, so the market route never walks back window by window

    def __init__(self, client: BseClient | None = None):
        self.bse = client or BseClient()

    async def __aenter__(self) -> BseEquity:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.bse.aclose()

    async def _json(self, path: str, params: dict[str, Any], cache_ttl: float | None = None) -> Any:
        data, _ = await self.bse.get_json(path, params, cache_ttl=cache_ttl)
        return data

    async def scrips(self) -> list[BseScrip]:
        rows = await self._json("/ListofScripData/w", {"Group": "", "Scripcode": "", "industry": "", "segment": "Equity",
                                                       "status": "Active"}, cache_ttl=SCRIP_LIST_TTL_S)  # fmt: skip
        return parse_scrip_list(rows if isinstance(rows, list) else [])

    async def quote(self, code: str) -> BseQuote | None:
        header = await self._json(
            "/getScripHeaderData/w", {"Debtflag": "", "scripcode": code, "seriesid": ""}
        )
        extra: list[Any] = []
        for path, params in (("/ComHeadernew/w", {"quotetype": "EQ", "scripcode": code, "seriesid": ""}),
                             ("/StockTrading/w", {"flag": "", "quotetype": "EQ", "scripcode": code}),
                             ("/HighLow/w", {"Type": "EQ", "flag": "C", "scripcode": code})):  # fmt: skip
            try:  # the header alone is a usable quote; the rest add ISIN, market cap and the 52-week range
                extra.append(await self._json(path, params))
            except BseError:
                extra.append(None)
        return build_quote(code, header or {}, *extra)

    async def history(self, code: str, start: date, end: date, series: str = "EQ") -> list[PriceBar]:
        url = f"{BSE_API}/StockPriceCSVDownload/w"
        params = {"pageType": "0", "rbType": "D", "Scode": code, "FDates": start.strftime("%d/%m/%Y"),
                  "TDates": end.strftime("%d/%m/%Y")}  # fmt: skip
        resp = await self.bse.http.get(url, params=params, headers=BSE_HEADERS)
        if not resp.ok:
            raise BseError(f"BSE HTTP {resp.status} for the price history of {code}")
        return [b for b in parse_price_csv(resp.text) if start <= b.day <= end]

    async def bhavcopy(self, day: date) -> list[BhavRow] | None:
        """The day's equity bhavcopy, or None when BSE has not published it (holiday, not yet out)."""
        url = BHAVCOPY_URL.format(day=day)
        resp = await self.bse.http.get(url, headers={**BSE_HEADERS, "Accept": "*/*", "Sec-Fetch-Site": "same-origin"},
                                       cache_ttl=BHAVCOPY_TTL_S, cache_if=lambda f: f.content[:7] == b"TradDt,")  # fmt: skip
        if resp.status == 404 or not resp.content.lstrip(b"\xef\xbb\xbf").startswith(b"TradDt,"):
            return None
        if not resp.ok:
            raise BseError(f"BSE HTTP {resp.status} for {url}")
        return parse_bhavcopy(resp.text)

    async def announcements(self, code: str, days: int = ANNOUNCEMENT_DAYS, today: date | None = None) -> list[Announcement]:  # fmt: skip
        end = today or datetime.now(IST).date()
        data = await self._json("/AnnSubCategoryGetData/w", {
            "pageno": "1", "strCat": "-1", "strPrevDate": (end - timedelta(days=days)).strftime("%Y%m%d"),
            "strScrip": code, "strSearch": "P", "strToDate": end.strftime("%Y%m%d"), "strType": "C", "subcategory": "-1"})  # fmt: skip
        return parse_announcements(code, data if isinstance(data, dict) else {})

    async def corporate_actions(self, code: str) -> list[CorporateAction]:
        rows = await self._json("/DefaultData/w", {"Fdate": "", "Purposecode": "", "TDate": "", "ddlcategorys": "E",
                                                   "ddlindustrys": "", "scripcode": code, "segment": "0",
                                                   "strSearch": "S"})  # fmt: skip
        return parse_corporate_actions(code, rows if isinstance(rows, list) else [])

    async def integrated_filings(
        self, code: str, kind: str = INTEGRATED_FINANCIALS
    ) -> list[IntegratedFiling]:
        if kind != INTEGRATED_FINANCIALS:
            return []
        data = await self._json("/Integratedfinancedata/w", {"scripcode": code})
        return parse_integrated_financials(code, data if isinstance(data, dict) else {})

    async def results(self, code: str, period: str = "Quarterly") -> list[ResultFiling]:
        """BSE's pre-2025 results filings have no XBRL index this adapter can read (see the module docstring)."""
        return []

    async def shareholding(self, code: str) -> list[Shareholding]:
        """Every filed pattern with its XBRL; the latest also carries promoter / public / employee-trust % from
        BSE's summary (older quarters' percentages come from their XBRL on the shareholding route)."""
        data = await self._json("/SHPQNewFormat/w", {"scripcode": code})
        rows = parse_shareholding_index(code, data if isinstance(data, dict) else {})
        try:
            end, pct = parse_shareholding_summary(
                await self._json("/CorporatesSHPSecuritybeta/w", {"scripcode": code, "qtrid": ""})
            )
        except BseError:
            return rows
        for r in rows:
            if end and r.as_of == end:
                r.promoter_pct, r.public_pct, r.employee_trusts_pct = (
                    pct["promoter"],
                    pct["public"],
                    pct["employee_trusts"],
                )
                break
        return rows

    async def fetch_bytes(self, url: str, *, cache_ttl: float | None = None) -> bytes:
        """GET a BSE filing file (XBRL instance). The site answers a missing file with its HTML shell and HTTP 200,
        so anything that is not XML is an error and is never cached."""
        if not official_file(url):
            raise BseError(f"not a BSE filing URL: {url}")
        resp = await self.bse.http.get(url, headers={**BSE_HEADERS, "Accept": "*/*", "Sec-Fetch-Site": "same-origin"},
                                       cache_ttl=cache_ttl, cache_if=lambda f: _is_xml(f.content))  # fmt: skip
        if not resp.ok:
            raise BseError(f"HTTP {resp.status} for {url}")
        if url.endswith(".xml") and not _is_xml(resp.content):
            raise BseError(f"BSE returned a page, not XBRL, for {url}")
        return resp.content


def _is_xml(body: bytes) -> bool:
    return body.lstrip(b"\xef\xbb\xbf \r\n\t")[:5] in (b"<?xml", b"<xbrl") or b"<xbrli:xbrl" in body[:600]

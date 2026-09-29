"""BSE public-issue adapter: BSE SME IPOs (issue list, details, category-wise subscription, offer documents).

Endpoints (api.bseindia.com/BseIndiaAPI/api, verified live 29-Sep-2026; JSON, no cookies needed):
- `/GetPublicIssue_par_updated/w?flag=1`             every open (Status L) and forthcoming (F) public issue:
                                                      IPO/FPO/rights/debt/buyback; `eXCHANGE_PLATFORM` = SME for SME
- `/GetMkt_ISSUE_BBS_IPO/w?IPO_NO=n`                  issue details: market lot, minimum bid, issue size, BRLM,
                                                      registrar and document links (market maker is not published)
- `/Pubissues_BSEDemSchd_GrShoe_ng/w?IPO_NO=n`        `is_green_shoe` S/BS marks the SME book-building format
- `/Pubissues_GetBkbldgCatdem_PAR_bbnew_ng/w?IPO_NO=n` SME category-wise demand (`table1`, `Maxdt` timestamp)
- `/Pubissues_IPODRHP_par_ng/w`                       offer documents (mainboard only; paths under /corporates/download/)

BSE's Akamai edge refuses requests without the full set of browser fetch headers (curl with the same headers gets a
403; httpx over HTTP/1.1 passes). There is no warm-up page that fixes a refusal, so a 403 or a non-JSON body is an
error. BSE SME issues trade only on BSE, so the BSE book is the whole book; `times` are BSE's own figures.
Document links on listing.bseindia.com sit behind a bot challenge and usually do not download from a script.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from finresearch.adapters.http import IST, Fetched, FetchRecord, PoliteClient
from finresearch.adapters.nse import (
    CategorySubscription,
    IpoDetail,
    SubscriptionSnapshot,
    _clean_text,
    parse_num,
    parse_price_band,
)

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_DOWNLOAD = "https://www.bseindia.com/corporates/download/"
BSE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
    "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}
BSE_RATE = 2.0  # requests per second to api.bseindia.com
DETAIL_TTL_S = 6 * 3600  # issue details change rarely; the radar may reuse them
SME_FLAGS = {"S", "BS"}  # is_green_shoe values for which BSE's own page uses the SME demand table


class BseError(RuntimeError):
    """BSE refused the request or returned something that is not the expected JSON."""


def detail_url(ipo_no: int | str) -> str:
    return f"{BSE_API}/GetMkt_ISSUE_BBS_IPO/w?IPO_NO={ipo_no}"


# --------------------------------------------------------------------------- parsing helpers
def parse_bse_timestamp(value: str | None) -> datetime | None:
    """'9/29/2026 10:35:43 AM' (IST) -> aware datetime; '' -> None."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=IST)
    except ValueError:
        return None


def _iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


_PERIOD = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})")


def parse_bse_period(value: str | None) -> tuple[date, date] | None:
    """'28 Sep 2026 to 30 Sep 2026' -> (open, close)."""
    found = _PERIOD.findall(value or "")
    if len(found) < 2:
        return None
    a, b = (datetime.strptime(" ".join(x), "%d %b %Y").date() for x in (found[0], found[-1]))
    return a, b


def _party(value: str | None) -> str | None:
    """'Shannon Advisors Private Limited^902, IX Floor...||||email|contact' -> the name."""
    name = _clean_text((value or "").split("^", 1)[0])
    return name or None


def _int(value: Any) -> int | None:
    n = parse_num(value)
    return int(n) if n is not None else None


# --------------------------------------------------------------------------- models
class BseIssue(BaseModel):
    """A row of GetPublicIssue_par_updated."""

    ipo_no: int
    scrip_code: str
    company: str
    issue_type: str | None = None  # IR_flag: IPO, FPO, RI (rights), DPI (debt), OTB, BuyBack
    platform: str | None = None  # SME, MainBoard, Debt
    status: str | None = None  # L = open (live), F = forthcoming
    issue_start: date | None = None
    issue_end: date | None = None
    price_band: str | None = None
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    face_value: Decimal | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def is_sme_ipo(self) -> bool:
        return self.issue_type == "IPO" and (self.platform or "").upper() == "SME"

    @classmethod
    def parse(cls, row: dict[str, Any]) -> BseIssue:
        low, high = parse_price_band(row.get("Price_Band"))
        return cls(ipo_no=int(row["IPO_NO"]), scrip_code=str(row.get("Scrip_cd") or "").strip(),
                   company=_clean_text(row.get("Scrip_Name")), issue_type=row.get("IR_flag"),
                   platform=row.get("eXCHANGE_PLATFORM"), status=row.get("Status"),
                   issue_start=_iso_date(row.get("Start_Dt")), issue_end=_iso_date(row.get("End_Dt")),
                   price_band=row.get("Price_Band"), price_low=low, price_high=high,
                   face_value=parse_num(row.get("Face_Val")), raw=row)  # fmt: skip


DOC_FIELDS = {"Prospectus_GID": "Red Herring Prospectus", "Price_Band_Advertisement": "Price Band Advertisement",
              "Addendum": "Addendum", "Corrigendum": "Corrigendum", "Anchor_Details": "Anchor Allocation Report"}  # fmt: skip


class BseIssueDetail(BaseModel):
    """GetMkt_ISSUE_BBS_IPO for one issue."""

    ipo_no: int
    scrip_code: str | None = None
    symbol: str | None = None
    company: str | None = None
    issue_open: date | None = None
    issue_close: date | None = None
    issue_period: str | None = None
    price_band: str | None = None
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    face_value: Decimal | None = None
    market_lot: int | None = None
    minimum_bid: int | None = None
    issue_size_shares: int | None = None
    lead_manager: str | None = None
    registrar: str | None = None
    documents: dict[str, str] = Field(default_factory=dict, description="label -> URL")
    as_of: datetime | None = None
    fetch: FetchRecord | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def min_lots(self) -> int | None:
        """Lots in the minimum bid: SME issues require 2 lots from individual investors since 2025."""
        if not self.market_lot or not self.minimum_bid:
            return None
        return self.minimum_bid // self.market_lot

    def issue_info(self) -> dict[str, str]:
        """The details in NSE's issue-information keys and formats, so baseline claims, watches and rules read
        BSE-only issues the same way."""
        out = {"Platform": "BSE SME", "Exchange": "BSE", "BSE IPO No": str(self.ipo_no)}
        if self.company:
            out["Company"] = self.company
        if self.symbol:
            out["Symbol"] = self.symbol
        if self.issue_open and self.issue_close:
            out["Issue Period"] = f"{self.issue_open:%d-%b-%Y} to {self.issue_close:%d-%b-%Y}"
        if self.price_low is not None and self.price_high is not None:
            lo, hi = (format(x.normalize(), "f") for x in (self.price_low, self.price_high))
            out["Price Range"] = f"Rs.{lo} to Rs.{hi}" if lo != hi else f"Rs.{hi}"
        if self.market_lot:
            out["Lot Size"] = f"{self.market_lot} Equity Shares"
        if self.minimum_bid:
            lots = f" ({self.min_lots} lots)" if self.min_lots else ""
            out["Minimum Bid"] = f"{self.minimum_bid} Equity Shares{lots}"
        if self.face_value is not None:
            out["Face Value"] = f"Rs. {format(self.face_value.normalize(), 'f')} per Equity Share"
        if self.issue_size_shares:
            out["Issue Size"] = f"{self.issue_size_shares} Equity Shares"
        if self.lead_manager:
            out["Book Running Lead Manager"] = self.lead_manager
        if self.registrar:
            out["Registrar"] = self.registrar
        out.update(self.documents)
        return out

    @classmethod
    def parse(cls, data: dict[str, Any], fetch: FetchRecord | None = None) -> BseIssueDetail:
        rows = data.get("IPONO_0") or []
        if not rows:
            raise BseError("issue details have no IPONO_0 row")
        r = rows[0]
        period = parse_bse_period(r.get("Issue_Period"))
        low, high = parse_price_band(r.get("Price_Band"))
        docs = {label: _clean_text(r.get(key)) for key, label in DOC_FIELDS.items()}
        return cls(ipo_no=int(r["IPO_NO"]), scrip_code=_clean_text(r.get("ScripCode")) or None,
                   symbol=_clean_text(r.get("Symbol")) or None, company=_clean_text(r.get("ScripName")) or None,
                   issue_open=period[0] if period else None, issue_close=period[1] if period else None,
                   issue_period=r.get("Issue_Period"), price_band=r.get("Price_Band"), price_low=low, price_high=high,
                   face_value=parse_num(r.get("Face_Value")), market_lot=_int(r.get("Market_Lot")),
                   minimum_bid=_int(r.get("Minimum_Bid_Quantity")),
                   issue_size_shares=_int(r.get("Issue_Size_No_of_shares")),
                   lead_manager=_party(r.get("Book_Running_Lead_Manager")), registrar=_party(r.get("Registrar")),
                   documents={k: v for k, v in docs.items() if v.startswith("http")},
                   as_of=parse_bse_timestamp(r.get("DT_TM")), fetch=fetch, raw=data)  # fmt: skip


def parse_sme_demand(symbol: str, data: dict[str, Any]) -> SubscriptionSnapshot:
    """SME category-wise demand (`table1`) -> the same snapshot model as NSE's combined table.

    Rows: 1 QIBs, 2 Non Institutional Investors (2.1 above ₹10 lakh, 2.2 up to ₹10 lakh), 3 Individual Investors
    (bidding for 2 lots), 4 employees, 5 shareholders, 6 policy holders, then the unnumbered Total. Codes follow
    NSE's srNo scheme, so `top_level` and the rules' qib/nii/rii matching work unchanged.
    """
    rows = [r for r in data.get("table1") or [] if r.get("SRNo") != "Sr.No." and r.get("col2") != "Category"]
    cats: list[CategorySubscription] = []
    for r in rows:
        code = str(r.get("SRNo") or "").strip()
        name = _clean_text(r.get("col2"))
        offered = parse_num(r.get("col3"))
        cats.append(CategorySubscription(name=name, code=code or None, shares_offered=offered,
                                         shares_bid=parse_num(r.get("col4")),
                                         times=parse_num(r.get("col5")) if offered else None))  # fmt: skip
    total = next((c for c in cats if c.is_total), None)
    stamp = next((r.get("Maxdt") for r in data.get("table1") or [] if r.get("Maxdt")), None)
    return SubscriptionSnapshot(symbol=symbol, as_of=parse_bse_timestamp(stamp), source="bse_sme", categories=cats,
                                total_times=total.times if total else None,
                                total_shares_offered=total.shares_offered if total else None,
                                total_shares_bid=total.shares_bid if total else None)  # fmt: skip


def as_ipo_detail(detail: BseIssueDetail, snapshot: SubscriptionSnapshot | None) -> IpoDetail:
    """BSE details plus demand as an NSE-shaped IpoDetail (series SME), for the monitor and the rules."""
    return IpoDetail(symbol=detail.symbol or str(detail.ipo_no), series="SME", company_name=detail.company,
                     combined=snapshot, issue_info=detail.issue_info(), fetch=detail.fetch)  # fmt: skip


class OfferDocument(BaseModel):
    """A row of Pubissues_IPODRHP_par_ng: DRHP / RHP / prospectus links of one issuer."""

    company: str
    updated: datetime | None = None
    drhp: str | None = None
    rhp: str | None = None
    prospectus: str | None = None

    @classmethod
    def parse(cls, row: dict[str, Any]) -> OfferDocument:
        def link(key: str) -> str | None:
            path = _clean_text(row.get(key))
            if not path:
                return None
            return path if path.startswith("http") else BSE_DOWNLOAD + path

        return cls(company=_clean_text(row.get("Scrip_Name")), updated=parse_bse_timestamp(row.get("updated_date")),
                   drhp=link("DRHP_Doc"), rhp=link("Red_Herring_Prospectus"), prospectus=link("Prospectus"))  # fmt: skip


# --------------------------------------------------------------------------- client
class BseClient:
    """BSE public-issue endpoints. Owns its PoliteClient unless one is passed in."""

    def __init__(self, client: PoliteClient | None = None) -> None:
        self._own = client is None
        self.http = client or PoliteClient(host_rates={"nseindia.com": 2.0, "bseindia.com": BSE_RATE})

    async def __aenter__(self) -> BseClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own:
            await self.http.aclose()

    async def get_json(self, path: str, params: dict[str, Any] | None = None,
                       cache_ttl: float | None = None) -> tuple[Any, Fetched]:  # fmt: skip
        url = f"{BSE_API}{path}"
        resp = await self.http.get(url, params=params, headers=BSE_HEADERS, cache_ttl=cache_ttl)
        if not resp.ok:
            raise BseError(f"BSE HTTP {resp.status} for {url}")
        if resp.content.lstrip()[:1] not in (b"{", b"["):
            raise BseError(f"BSE returned a non-JSON page for {url}")
        try:
            return resp.json(), resp
        except ValueError as exc:
            raise BseError(f"BSE returned invalid JSON for {url}") from exc

    async def public_issues(self) -> list[BseIssue]:
        data, _ = await self.get_json("/GetPublicIssue_par_updated/w", {"flag": 1})
        return [BseIssue.parse(r) for r in (data or {}).get("Table") or [] if r.get("IPO_NO") is not None]

    async def sme_issues(self) -> list[BseIssue]:
        return [i for i in await self.public_issues() if i.is_sme_ipo]

    async def issue_detail(self, ipo_no: int | str, *, cache_ttl: float | None = None) -> BseIssueDetail:
        data, resp = await self.get_json("/GetMkt_ISSUE_BBS_IPO/w", {"IPO_NO": ipo_no}, cache_ttl=cache_ttl)
        if not isinstance(data, dict):
            raise BseError(f"unexpected issue-details payload for IPO {ipo_no}")
        return BseIssueDetail.parse(data, fetch=resp.record)

    async def sme_subscription(self, ipo_no: int | str, symbol: str | None = None) -> SubscriptionSnapshot:
        """Category-wise demand of an SME issue. BSE's page reads the demand schedule first and uses this table only
        for SME (S/BS) issues; a mainboard issue's book is NSE's combined table."""
        sched, _ = await self.get_json("/Pubissues_BSEDemSchd_GrShoe_ng/w", {"IPO_NO": ipo_no})
        rows = (sched or {}).get("table") or []
        if not rows:  # no demand schedule before bidding opens: an empty book, not an error
            return parse_sme_demand(symbol or str(ipo_no), {})
        flag = str(rows[0].get("is_green_shoe") or "").strip()
        if flag not in SME_FLAGS:
            raise BseError(f"IPO {ipo_no} is not in BSE's SME demand format (flag {flag!r})")
        data, _ = await self.get_json("/Pubissues_GetBkbldgCatdem_PAR_bbnew_ng/w", {"IPO_NO": ipo_no})
        return parse_sme_demand(symbol or str(ipo_no), data or {})

    async def ipo_detail(self, ipo_no: int | str) -> IpoDetail:
        """Details and live demand as an NSE-shaped IpoDetail (what the monitor and the rules consume)."""
        detail = await self.issue_detail(ipo_no)
        snap = await self.sme_subscription(ipo_no, detail.symbol)
        return as_ipo_detail(detail, snap)

    async def offer_documents(self) -> list[OfferDocument]:
        data, _ = await self.get_json("/Pubissues_IPODRHP_par_ng/w", cache_ttl=DETAIL_TTL_S)
        return [OfferDocument.parse(r) for r in (data or {}).get("table") or []]


# --------------------------------------------------------------------------- radar
_STOP = {"limited", "ltd", "private", "pvt", "the", "and", "co", "company"}


def name_key(name: str) -> frozenset[str]:
    """Normalised company name for matching one issue across exchanges ('Shivchem Agro Ltd.' == 'SHIVCHEM AGRO
    LIMITED'). Exact token-set equality, not a subset test: SME names often share a word ('Acme ...')."""
    return frozenset(t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in _STOP)


async def sme_radar(
    client: BseClient | None = None,
) -> tuple[list[tuple[BseIssue, BseIssueDetail | None]], list[str]]:
    """Open and forthcoming BSE SME IPOs with their details (lot, minimum bid); details are cached for hours."""
    errors: list[str] = []
    out: list[tuple[BseIssue, BseIssueDetail | None]] = []
    async with client or BseClient() as bse:
        for issue in await bse.sme_issues():
            try:
                detail = await bse.issue_detail(issue.ipo_no, cache_ttl=DETAIL_TTL_S)
            except Exception as e:  # the row is still worth listing without its lot
                detail = None
                errors.append(f"BSE IPO {issue.ipo_no} details: {e}"[:200])
            out.append((issue, detail))
    return out, errors

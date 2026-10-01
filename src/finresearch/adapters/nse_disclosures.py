"""NSE disclosure feeds: surveillance lists (ASM, GSM), the F&O ban list, promoter encumbrance (pledge), insider
trades (SEBI PIT Regulation 7), substantial acquisitions (SEBI SAST Regulation 29), bulk and block deals, and credit
ratings filed by listed issuers. One adapter so every feed shares one cookie session, one rate limit and one set of
parsers. Shapes were recorded from polite live requests on 01-Oct-2026 (field names only; the test fixtures in
tests/fixtures/nse/disclosures/ carry synthetic values).

Endpoints (all JSON after the usual cookie warm-up unless noted):
- `/api/reportASM`                          {"longterm": {"data": [...]}, "shortterm": {"data": [...]}}; each row has
                                            symbol, isin, `asmSurvIndicator` ("Stage I"), `survCode` ("LTASM - I (13)")
- `/api/reportGSM`                          [...] rows with symbol, isin, `gsmStage`, `survCode`. `gsmStage` is NOT the
                                            stage: for "IBC - Receipt & GSM 0 (62)" it reads "LXII" (the code number in
                                            roman numerals), so the stage is parsed from `survCode`
- nsearchives `/content/fo/fo_secban.csv`   "Securities in Ban For Trade Date 01-OCT-2026:" then "1,SYMBOL" lines
- `/api/corporate-pledgedata?index=equities&symbol=X`  one row (the latest quarter `shp`). Checked against all 1,544
                                            rows of the market-wide file: `percPromoterHolding` = totPromoterHolding /
                                            totIssuedShares (1525/1525), `percPromoterShares` = totPromoterShares /
                                            totPromoterHolding (457/457) and `percTotShares` = totPromoterShares /
                                            totIssuedShares (457/457): the quarter-end promoter encumbrance.
                                            `numSharesPledged` / `percSharesPledged` are the event disclosures since,
                                            whose base is unclear (`percSharesPledged` matched numSharesPledged /
                                            totIssuedShares in only 1096/1540 rows, and numSharesPledged can exceed the
                                            promoter holding), so they are shown as NSE reports them, never computed on
- `/api/corporates-pit-gg?index=equities&symbol=X&from_date=dd-mm-yyyy&to_date=dd-mm-yyyy`  the PIT filing index
                                            NSE's insider-trading page itself calls (corporate-filings.js, 01-Oct-2026):
                                            one row per filing with `xmlFileName` (the filing's XBRL), `appId`,
                                            `typeOfSubmission`, `prevAppId`. The quantities are only in the XBRL
                                            (in-bse-co taxonomy). The older `/api/corporates-pit` returns a fixed set of
                                            20 rows per symbol (2018-2021 for one large stock) and ignores date
                                            parameters, so it is NOT used: it would pass years-old trades off as recent
- `/api/corporate-sast-reg29?index=equities&symbol=X`  the latest 20 Regulation 29 disclosures
- `/api/historicalOR/bulk-block-short-deals?optionType=bulk_deals|block_deals&symbol=X&from=dd-mm-yyyy&to=...`
                                            {"data": [...]} with BD_DT_DATE, BD_SYMBOL, BD_CLIENT_NAME, BD_BUY_SELL,
                                            BD_QTY_TRD, BD_TP_WATP (price), BD_REMARKS
- `/api/corporate-credit-rating?index=equities`  ~430 recent rating filings (a symbol parameter is ignored). Columns
                                            NameOfCRAgency, CreditRating, RatingAction (only "Reaffirm" / "New" /
                                            "Other"), SpecifyOthRatingActn (the real verb when "Other": "Assigned",
                                            "Withdrawn", ...), Outlook (free text, mixed case), DateofCR ("29-09-2026"),
                                            BroadcastDateTime ("01-OCT-2026 11:57:55"), ISIN, and the *Earlier columns

ToS and politeness: these are the website's own endpoints for personal, low-rate use. Market-wide lists are read
once a day; per-stock feeds only for held and watched stocks (monitor.disclosures) or a stock page someone opens.
Every response is size-capped; a failure raises and is recorded as "unavailable", never as an empty list
(DATA-004). Personal identifiers (PAN, Aadhaar-like numbers, DIN) are scrubbed from every free-text field before it
is returned (`scrub`).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree as ET

from pydantic import BaseModel, Field

from finresearch.adapters.http import IST, Fetched, PoliteClient
from finresearch.adapters.nse import API_HEADERS, NSE_BASE, NseClient, NseError, parse_num

ARCHIVES = "https://nsearchives.nseindia.com"
INSIDER_PAGE = f"{NSE_BASE}/companies-listing/corporate-filings-insider-trading"
SURVEILLANCE_PAGE = f"{NSE_BASE}/reports/asm"
PLEDGE_PAGE = f"{NSE_BASE}/companies-listing/corporate-filings-pledged-data"
SAST_PAGE = f"{NSE_BASE}/companies-listing/corporate-filings-regulation-29"
RATING_PAGE = f"{NSE_BASE}/companies-listing/corporate-filings-credit-rating"
DEALS_PAGE = f"{NSE_BASE}/report-detail/display-bulk-and-block-deals"
FNO_BAN_URL = f"{ARCHIVES}/content/fo/fo_secban.csv"
FNO_BAN_PAGE = f"{NSE_BASE}/market-data/securities-in-ban-period"

# size caps per response (bytes): the largest seen on 01-Oct-2026 was the credit-rating list at 498 KB and the
# market-wide pledge file at 964 KB; anything far larger is not the expected payload and is refused, not parsed
MAX_JSON_BYTES = 8_000_000
MAX_XBRL_BYTES = 2_000_000
MAX_CSV_BYTES = 200_000
XBRL_CACHE_S = 30 * 86400.0  # a filing's XBRL never changes (a revision is a new filing with its own file)


# --------------------------------------------------------------------------- privacy
# PAN: 5 letters, 4 digits, 1 letter (Income-tax Rule 114; SEBI's RSS prints e.g. "(PAN: ABCDE1234F)").
PAN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
_PAN_LABELLED = re.compile(
    r"\(?\s*PAN\s*(?:No\.?|Number)?\s*[:\-]?\s*\[?(?:[A-Z]{5}[0-9]{4}[A-Z])\]?\s*\)?", re.I
)
_AADHAAR = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")  # 12-digit UIDAI number, with or without spaces
_DIN = re.compile(r"\bDIN\s*(?:No\.?)?\s*[:\-]?\s*\d{8}\b", re.I)  # director identification number


def scrub(text: Any) -> str | None:
    """Free text with personal identifiers removed (PAN, Aadhaar-like 12-digit numbers, DIN) and spaces normalised.
    Applied to every string a feed returns before anything is stored or shown."""
    if text is None:
        return None
    s = str(text)
    s = _PAN_LABELLED.sub(" ", s)
    s = PAN.sub("[removed]", s)
    s = _AADHAAR.sub("[removed]", s)
    s = _DIN.sub(" ", s)
    s = re.sub(r"\(\s*\)", "", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- dates
_DATE_FORMATS = (
    "%d-%m-%Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d %b %Y",
    "%d %b, %Y",
    "%d %B %Y",
)
_DT_FORMATS = (
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def parse_day(value: Any) -> date | None:
    """A date in any of the formats these feeds use: "29-09-2026" (credit ratings), "30-SEP-2026" / "30-Sep-2026"
    (deals, SAST), "2026-09-30" (XBRL), "30 Sep, 2026" (SEBI RSS). A range "23-SEP-2026 to 25-SEP-2026" gives its
    first day; a timestamp gives its day. None for blanks and "-"."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("-", "NA", "None", "null"):
        return None
    text = re.split(r"\s+to\s+", text, maxsplit=1, flags=re.I)[0].strip()
    text = re.sub(
        r"\s+[+-]\d{4}$", "", text
    )  # "30 Sep, 2026 +0530" (a space before the offset: "01-OCT-2026" keeps its year)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()  # %b / %B match any case ("SEP", "Sep")
        except ValueError:
            continue
    dt = parse_when(text)
    return dt.date() if dt else None


def parse_when(value: Any) -> datetime | None:
    """A publisher timestamp ("01-OCT-2026 11:57:55", "30-Sep-2026 23:44:11") as IST."""
    if value is None:
        return None
    text = str(value).strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _txt(v: Any) -> str | None:
    s = scrub(v)
    return s if s and s not in ("-", "NA", "None", "null") else None


# --------------------------------------------------------------------------- surveillance (ASM / GSM)
class Surveillance(BaseModel):
    """One security on an NSE surveillance list."""

    framework: str  # "ASM" | "GSM"
    symbol: str
    isin: str | None
    company: str | None = None
    term: str | None = None  # ASM: "long-term" | "short-term" list
    stage: str | None = None  # "I", "II", "IV" (ASM) or "0", "I".."VI" (GSM)
    code: str | None = None  # NSE's survCode as published, e.g. "IBC - Receipt & GSM 0 (62)"
    components: list[str] = Field(default_factory=list)  # parsed survCode parts: ["IBC Receipt", "GSM 0"]
    ibc: bool = False  # insolvency (IBC) flag in the code: a resolution process has been admitted/initiated
    esm: str | None = None  # Enhanced Surveillance Measure stage, when part of the code

    @property
    def label(self) -> str:
        if self.framework == "ASM":
            return f"ASM {self.term or ''} Stage {self.stage or '?'}".replace("  ", " ")
        parts = [f"GSM Stage {self.stage}" if self.stage is not None else "GSM"]
        if self.esm:
            parts.append(f"ESM Stage {self.esm}")
        if self.ibc:
            parts.append("IBC")
        return " · ".join(parts)


_ROMAN = re.compile(r"^(?:0|I|II|III|IV|V|VI|VII|VIII|IX|X)$")


def parse_surv_code(code: str | None) -> dict[str, Any]:
    """NSE's compound survCode -> components. "IBC - Receipt & GSM 0 (62)" -> GSM stage 0, IBC; "ESM II & GSM 0
    (37)" -> GSM 0, ESM II; "GSM IV & IBC - Receipt (66)" -> GSM IV, IBC; "LTASM - I (13)" -> LTASM I."""
    out: dict[str, Any] = {
        "components": [],
        "gsm": None,
        "esm": None,
        "ibc": False,
        "ltasm": None,
        "stasm": None,
    }
    if not code:
        return out
    body = re.sub(r"\(\s*\d+\s*\)\s*$", "", str(code)).strip()
    for part in [p.strip() for p in body.split("&") if p.strip()]:
        norm = re.sub(r"\s*-\s*", " ", part).strip()
        out["components"].append(norm)
        m = re.match(r"^(GSM|ESM|LTASM|STASM)\s+([0IVX]+)$", norm, re.I)
        if m and _ROMAN.match(m.group(2).upper()):
            out[m.group(1).lower()] = m.group(2).upper()
        elif norm.upper().startswith("IBC"):
            out["ibc"] = True
    return out


def parse_asm(data: Any) -> list[Surveillance]:
    if not isinstance(data, dict):
        raise NseError(f"ASM report is not an object: {type(data).__name__}")
    out = []
    for key, label in (("longterm", "long-term"), ("shortterm", "short-term")):
        block = data.get(key) or {}
        rows = block.get("data") if isinstance(block, dict) else block
        for r in rows or []:
            sym = str(r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            ind = str(r.get("asmSurvIndicator") or "").strip()
            stage = ind.split()[-1].upper() if ind.lower().startswith("stage") else None
            pc = parse_surv_code(r.get("survCode"))
            stage = stage or pc["ltasm"] or pc["stasm"]
            out.append(Surveillance(framework="ASM", symbol=sym, isin=_isin(r.get("isin")), company=_txt(r.get("companyName")),
                                    term=label, stage=stage, code=_txt(r.get("survCode")), components=pc["components"]))  # fmt: skip
    return out


def parse_gsm(data: Any) -> list[Surveillance]:
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise NseError(f"GSM report is not a list: {type(data).__name__}")
    out = []
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        pc = parse_surv_code(r.get("survCode"))
        stage = pc["gsm"]
        if stage is None and not r.get(
            "survCode"
        ):  # no code at all: gsmStage, when it is a real stage (0..VI)
            g = str(r.get("gsmStage") or "").strip().upper()
            stage = g if g in ("0", "I", "II", "III", "IV", "V", "VI") else None
        out.append(Surveillance(framework="GSM", symbol=sym, isin=_isin(r.get("isin")), company=_txt(r.get("companyName")),
                                stage=stage, code=_txt(r.get("survCode")), components=pc["components"],
                                ibc=pc["ibc"], esm=pc["esm"]))  # fmt: skip
    return out


def _isin(v: Any) -> str | None:
    s = str(v or "").strip().upper()
    return s if re.fullmatch(r"IN[A-Z0-9]{9}[0-9]", s) else None


# --------------------------------------------------------------------------- F&O ban
class FnoBan(BaseModel):
    trade_date: date
    symbols: list[str]


_BAN_HEAD = re.compile(r"Securities in Ban For Trade Date\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", re.I)


def parse_fno_ban(text: str) -> FnoBan:
    """The ban CSV. A body without the "Securities in Ban For Trade Date" header (an HTML shell on a holiday, a block
    page) raises: it is "not published", never "no stock is banned"."""
    m = _BAN_HEAD.search(text[:500] if text else "")
    if not m:
        raise NseError(
            "F&O ban file has no 'Securities in Ban For Trade Date' header (not published or a block page)"
        )
    day = parse_day(m.group(1))
    if day is None:
        raise NseError(f"F&O ban file has an unreadable trade date {m.group(1)!r}")
    symbols = []
    for line in text[m.end() :].splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) >= 2 and cells[0].isdigit() and re.fullmatch(r"[A-Z0-9&\-]+", cells[1].upper()):
            symbols.append(cells[1].upper())
    return FnoBan(trade_date=day, symbols=sorted(set(symbols)))


# --------------------------------------------------------------------------- pledge (SAST Regulation 31 encumbrance)
class Pledge(BaseModel):
    """Promoter encumbrance for one company and quarter. Percentages are recomputed from share counts (Decimal);
    `reported_*` keep NSE's own figures and `mismatch` says when the two disagree by more than 0.01 pp."""

    symbol: str
    company: str | None = None
    quarter_end: date | None  # `shp`: the shareholding quarter the encumbrance is from
    broadcast_at: datetime | None = None
    issued_shares: Decimal | None = None
    promoter_shares: Decimal | None = None  # totPromoterHolding
    promoter_pct: Decimal | None = None  # promoter holding, % of issued shares
    encumbered_shares: Decimal | None = None  # totPromoterShares
    pct_of_promoter: Decimal | None = None  # encumbered / promoter holding x 100
    pct_of_equity: Decimal | None = None  # encumbered / issued shares x 100
    reported_pct_of_promoter: Decimal | None = None  # percPromoterShares
    reported_pct_of_equity: Decimal | None = None  # percTotShares
    disclosed_shares: Decimal | None = (
        None  # numSharesPledged (event disclosures; base unclear, see module doc)
    )
    disclosed_pct_reported: Decimal | None = None  # percSharesPledged as NSE prints it
    mismatch: str | None = None


def _pct(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or not b:
        return None
    return (a * 100 / b).quantize(Decimal("0.0001"))


def parse_pledge(data: Any, symbol: str) -> Pledge | None:
    """The per-symbol pledge payload ({"data": [row]}) -> the latest quarter's row, or None when NSE has no row for
    the company (an empty list from a 200 answer: NSE's "no record", which the caller reports as such)."""
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise NseError(f"pledge payload is not a list: {type(data).__name__}")
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: parse_day(r.get("shp")) or date.min, reverse=True)
    r = rows[0]
    issued = parse_num(r.get("totIssuedShares"))
    prom = parse_num(r.get("totPromoterHolding"))
    enc = parse_num(r.get("totPromoterShares"))
    p = Pledge(symbol=symbol.upper(), company=_txt(r.get("comName")), quarter_end=parse_day(r.get("shp")),
               broadcast_at=parse_when(r.get("broadcastDt")), issued_shares=issued, promoter_shares=prom,
               promoter_pct=_pct(prom, issued), encumbered_shares=enc, pct_of_promoter=_pct(enc, prom),
               pct_of_equity=_pct(enc, issued), reported_pct_of_promoter=parse_num(r.get("percPromoterShares")),
               reported_pct_of_equity=parse_num(r.get("percTotShares")),
               disclosed_shares=parse_num(r.get("numSharesPledged")),
               disclosed_pct_reported=parse_num(r.get("percSharesPledged")))  # fmt: skip
    if prom == 0 and enc == 0:
        p.pct_of_promoter = Decimal(0)  # no promoter holding and nothing encumbered: 0, not unknown
    bad = []
    for mine, theirs, what in ((p.pct_of_promoter, p.reported_pct_of_promoter, "% of promoter holding"),
                               (p.pct_of_equity, p.reported_pct_of_equity, "% of equity")):  # fmt: skip
        if mine is not None and theirs is not None and abs(mine - theirs) > Decimal("0.01"):
            bad.append(f"{what}: recomputed {mine:.2f} vs NSE {theirs:.2f}")
    p.mismatch = "; ".join(bad) or None
    return p


# --------------------------------------------------------------------------- insider trades (PIT Regulation 7)
class PitFiling(BaseModel):
    """One row of the PIT filing index."""

    app_id: str | None
    prev_app_id: str | None = None
    symbol: str
    regulation: str | None = None
    submission: str | None = None  # "Original" | "Revised"
    broadcast_at: datetime | None = None
    xml_url: str | None = None
    html_url: str | None = None


def parse_pit_index(data: Any) -> list[PitFiling]:
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise NseError(f"PIT index payload is not a list: {type(data).__name__}")
    out = []
    for r in rows:
        xml = str(r.get("xmlFileName") or "").strip()
        html = str(r.get("ixbrl") or "").strip()
        prev = str(r.get("prevAppId") or "").strip()
        out.append(PitFiling(app_id=str(r.get("appId") or "").strip() or None,
                             prev_app_id=prev if prev and prev not in ("None", "-", "null") else None,
                             symbol=str(r.get("symbol") or "").strip().upper(), regulation=_txt(r.get("regulation")),
                             submission=_txt(r.get("typeOfSubmission")), broadcast_at=parse_when(r.get("broadcastDateTime")),
                             xml_url=xml if xml.startswith("https://") else None,
                             html_url=html if html.startswith("https://") else None))  # fmt: skip
    return out


class InsiderTxn(BaseModel):
    """One person's transaction from a PIT filing. `value_inr` is the consideration as filed, in rupees."""

    symbol: str | None
    isin: str | None = None
    person: str | None = None
    category: str | None = None  # "Promoter", "Promoter Group", "Director", "Key Managerial Personnel", ...
    instrument: str | None = None  # "Equity", "Warrants", "Derivatives", ...
    side: str | None = None  # "Buy" | "Sell" | "Pledge Revoke" | ... (as filed)
    mode: str | None = None  # "Market Purchase", "Market Sale", "ESOP", "Gift", "Inter-se Transfer", ...
    quantity: Decimal | None = None
    value_inr: Decimal | None = None
    held_before: Decimal | None = None
    held_after: Decimal | None = None
    from_day: date | None = None
    to_day: date | None = None
    intimated: date | None = None
    filed: date | None = None
    exchange: str | None = None
    regulation: str | None = None


_XBRL_FIELDS = {
    "TypeOfInstrument": "instrument", "CategoryOfPerson": "category", "NameOfThePerson": "person",
    "SecuritiesAcquiredOrDisposedNumberOfSecurity": "quantity",
    "SecuritiesAcquiredOrDisposedValueOfSecurity": "value_inr",
    "SecuritiesAcquiredOrDisposedTransactionType": "side",
    "SecuritiesHeldPriorToAcquisitionOrDisposalNumberOfSecurity": "held_before",
    "SecuritiesHeldPostAcquistionOrDisposalNumberOfSecurity": "held_after",
    "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate": "from_day",
    "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate": "to_day",
    "ModeOfAcquisitionOrDisposal": "mode", "DateOfIntimationToCompany": "intimated",
    "ExchangeOnWhichTheTradeWasExecuted": "exchange",
}  # fmt: skip
_NUM_FIELDS = {"quantity", "value_inr", "held_before", "held_after"}
_DAY_FIELDS = {"from_day", "to_day", "intimated"}


def parse_pit_xbrl(content: bytes) -> list[InsiderTxn]:
    """A PIT filing's XBRL (in-bse-co taxonomy, "PIT V2.0") -> one InsiderTxn per disclosure context. Facts are
    grouped by contextRef: the filing-level context carries Symbol / ISINCode / DateOfFiling, each "Disclosure<n>"
    context one person's transaction."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise NseError(f"PIT XBRL is not well-formed XML: {e}") from e
    by_ctx: dict[str, dict[str, str]] = {}
    for el in root:
        ctx = el.get("contextRef")
        if ctx is None:
            continue
        name = el.tag.rsplit("}", 1)[-1]
        by_ctx.setdefault(ctx, {})[name] = (el.text or "").strip()
    head = next((f for f in by_ctx.values() if "Symbol" in f or "ISINCode" in f), {})
    out = []
    for facts in by_ctx.values():
        if not any(k in facts for k in ("SecuritiesAcquiredOrDisposedNumberOfSecurity", "TypeOfInstrument")):
            continue
        kw: dict[str, Any] = {}
        for tag, field in _XBRL_FIELDS.items():
            if tag not in facts:
                continue
            v = facts[tag]
            kw[field] = (
                parse_num(v) if field in _NUM_FIELDS else parse_day(v) if field in _DAY_FIELDS else _txt(v)
            )
        sym = (head.get("Symbol") or "").strip().upper()
        out.append(InsiderTxn(symbol=sym if sym and sym not in ("NOTLISTED", "NA") else None,
                              isin=_isin(head.get("ISINCode")), filed=parse_day(head.get("DateOfFiling")),
                              regulation=_txt(head.get("DisclosureUnderRegulation")), **kw))  # fmt: skip
    return out


# --------------------------------------------------------------------------- SAST Regulation 29
class SastTxn(BaseModel):
    symbol: str
    acquirer: str | None = None
    kind: str | None = None  # "Acquisition" | "Sale"
    regulation: str | None = None  # "Reg29(1)" (crossing 5 %) | "Reg29(2)" (a 2 % change for a 5 %+ holder)
    promoter: bool | None = None
    mode: str | None = None
    day: date | None = None  # first day of the acquisition/sale range
    shares: Decimal | None = None
    pct: Decimal | None = None  # % of total shares acquired or sold
    pct_after: Decimal | None = None  # % held after
    filed_at: datetime | None = None
    attachment: str | None = None


def parse_sast(data: Any) -> list[SastTxn]:
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise NseError(f"SAST payload is not a list: {type(data).__name__}")
    out = []
    for r in rows:
        kind = _txt(r.get("acqSaleType"))
        sale = (kind or "").lower().startswith("sale")
        att = str(r.get("attachement") or "").strip()
        promo = str(r.get("promoterType") or "").strip().upper()
        out.append(SastTxn(symbol=str(r.get("symbol") or "").strip().upper(), acquirer=_txt(r.get("acquirerName")),
                           kind=kind, regulation=_txt(r.get("regType")),
                           promoter=True if promo == "Y" else False if promo == "N" else None,
                           mode=_txt(r.get("acquisitionMode")), day=parse_day(r.get("acquirerDate")),
                           shares=parse_num(r.get("noOfShareSale") if sale else r.get("noOfShareAcq")),
                           pct=parse_num(r.get("totSaleShare") if sale else r.get("totAcqShare")),
                           pct_after=parse_num(r.get("totAftShare")),
                           filed_at=parse_when(r.get("timestamp")) or parse_when(r.get("time")),
                           attachment=att if att.startswith("https://") else None))  # fmt: skip
    return out


# --------------------------------------------------------------------------- bulk and block deals
class Deal(BaseModel):
    kind: str  # "bulk" | "block"
    day: date | None
    symbol: str
    client: str | None = None
    side: str | None = None  # "BUY" | "SELL"
    quantity: Decimal | None = None
    price: Decimal | None = None  # trade price / weighted average price, ₹ per share
    remarks: str | None = None

    @property
    def value_inr(self) -> Decimal | None:
        return None if self.quantity is None or self.price is None else self.quantity * self.price


def parse_deals(data: Any, kind: str) -> list[Deal]:
    """Historical bulk/block deals ({"data": [...]}; keys BD_* for bulk deals). Keys are matched after their prefix
    ("BD_QTY_TRD" -> "QTY_TRD"), so the block-deal variant reads the same way."""
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise NseError(f"{kind} deals payload is not a list: {type(data).__name__}")
    out = []
    for r in rows:
        f = {re.sub(r"^[A-Z]{2,3}_", "", str(k).upper()): v for k, v in r.items()}
        side = str(f.get("BUY_SELL") or "").strip().upper() or None
        out.append(Deal(kind=kind, day=parse_day(f.get("DT_DATE")), symbol=str(f.get("SYMBOL") or "").strip().upper(),
                        client=_txt(f.get("CLIENT_NAME")), side=side, quantity=parse_num(f.get("QTY_TRD")),
                        price=parse_num(f.get("TP_WATP")), remarks=_txt(f.get("REMARKS"))))  # fmt: skip
    return out


# --------------------------------------------------------------------------- credit ratings
class RatingAction(BaseModel):
    """One credit-rating filing by a listed issuer, with the action classified (see `classify_rating`)."""

    isin: str | None
    symbol: str | None = (
        None  # NSE symbol when the issuer's equity is listed (NSE often prints "NOTLISTED"/"NA")
    )
    company: str | None = None
    agency: str | None = None
    rating: str | None = None
    outlook: str | None = None  # normalised: "Stable" | "Positive" | "Negative" | "Developing" | None
    watch: str | None = None  # "Negative" | "Positive" | "Developing" when on rating watch
    action_raw: str | None = None  # RatingAction + SpecifyOthRatingActn as filed
    action: str  # see ACTIONS
    adverse: bool = False
    rating_day: date | None = None  # DateofCR: the agency's rating date
    broadcast_at: datetime | None = None
    earlier_agency: str | None = None
    earlier_rating: str | None = None
    earlier_outlook: str | None = None
    derived: str | None = None  # how `action` was derived when the filing did not say it


ACTIONS = ("upgrade", "downgrade", "watch_negative", "watch_positive", "watch_developing", "outlook_negative",
           "outlook_positive", "default", "not_cooperating", "withdrawn", "reaffirm", "new", "other")  # fmt: skip
ADVERSE = {"downgrade", "watch_negative", "outlook_negative", "default", "not_cooperating"}

# rating scales (SEBI's uniform rating symbols, Master Circular for CRAs: long-term AAA..D, short-term A1..D;
# "+"/"-" modifiers). Lower index = higher credit quality.
LONG_SCALE = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-",
              "C+", "C", "C-", "D"]  # fmt: skip
SHORT_SCALE = ["A1+", "A1", "A2+", "A2", "A3+", "A3", "A4+", "A4", "D"]
_AGENCY_PREFIX = re.compile(
    r"\b(?:CARE|IND|CRISIL|Crisil|ICRA|\[ICRA\]|BWR|ACUITE|Acuite|IVR|PP-MLD|Provisional)\b\s*"
)
_SYMBOL = re.compile(r"(?<![A-Za-z0-9])(A1\+|A1|A2\+|A2|A3\+|A3|A4\+|A4|AAA|AA[+-]?|BBB[+-]?|BB[+-]?|A[+-]?|B[+-]?|C[+-]?|D)"
                     r"(?![A-Za-z0-9+\-])")  # fmt: skip


def rating_notch(rating: str | None) -> tuple[str, int] | None:
    """("long", index) or ("short", index) of the first rating symbol in a rating string, preferring a long-term
    symbol: "IND A-/Stable/IND A2+" -> ("long", 6). None when no symbol is found."""
    if not rating:
        return None
    text = _AGENCY_PREFIX.sub(" ", str(rating))
    syms = _SYMBOL.findall(text)
    for s in syms:
        if s in LONG_SCALE:
            return "long", LONG_SCALE.index(s)
    for s in syms:
        if s in SHORT_SCALE:
            return "short", SHORT_SCALE.index(s)
    return None


def _outlook_of(*texts: str | None) -> str | None:
    for t in texts:
        low = (t or "").lower()
        if "watch" in low or "rwn" in low.split() or "cwn" in low.split():
            continue
        for word in ("negative", "positive", "developing", "stable"):
            if re.search(rf"\b{word}\b", low):
                return word.title()
    return None


def _watch_of(*texts: str | None) -> str | None:
    for t in texts:
        low = (t or "").lower()
        if re.search(r"\b(rwn|cwn)\b", low) or re.search(r"watch[^;/]*negative", low):
            return "Negative"
        if re.search(r"\b(rwp|cwp)\b", low) or re.search(r"watch[^;/]*positive", low):
            return "Positive"
        if re.search(r"watch[^;/]*developing", low):
            return "Developing"
    return None


def classify_rating(action: str | None, other: str | None, rating: str | None, outlook: str | None,
                    agency: str | None, earlier_rating: str | None, earlier_outlook: str | None,
                    earlier_agency: str | None) -> tuple[str, bool, str | None]:  # fmt: skip
    """(action, adverse, derived-from note). The filing's verb comes first (RatingAction, or SpecifyOthRatingActn
    when RatingAction is "Other"); a bare "Reaffirm"/"New"/"Other" is refined by comparing the rating notch with the
    earlier rating by the SAME agency (another agency's earlier rating is not comparable)."""
    verb = f"{action or ''} {other or ''}".lower()
    text = f"{rating or ''} {outlook or ''}".lower()
    watch = _watch_of(rating, outlook)
    now_notch = rating_notch(rating)
    if "downgrad" in verb:
        kind = "downgrade"
    elif "upgrad" in verb:
        kind = "upgrade"
    elif "not cooperat" in verb or "non-cooperat" in verb or re.search(r"\binc\b|not cooperating", text):
        kind = "not_cooperating"
    elif "withdr" in verb:
        kind = "withdrawn"
    elif now_notch and now_notch[1] == len(LONG_SCALE if now_notch[0] == "long" else SHORT_SCALE) - 1:
        kind = "default"  # rated D
    elif watch:
        kind = f"watch_{watch.lower()}"
    elif "reaffirm" in verb:
        kind = "reaffirm"
    elif "new" in verb or "assign" in verb:
        kind = "new"
    else:
        kind = "other"
    derived = None
    same_agency = bool(agency and earlier_agency and _agency_key(agency) == _agency_key(earlier_agency))
    if kind in ("reaffirm", "other", "new") and same_agency:
        before = rating_notch(earlier_rating)
        if now_notch and before and now_notch[0] == before[0] and now_notch[1] != before[1]:
            kind = "downgrade" if now_notch[1] > before[1] else "upgrade"
            derived = f"derived: {earlier_rating} -> {rating} (same agency)"
        elif kind == "reaffirm":
            o_now, o_before = _outlook_of(outlook, rating), _outlook_of(earlier_outlook, earlier_rating)
            if o_now == "Negative" and o_before and o_before != "Negative":
                kind, derived = "outlook_negative", f"derived: outlook {o_before} -> Negative"
            elif o_now == "Positive" and o_before and o_before != "Positive":
                kind, derived = "outlook_positive", f"derived: outlook {o_before} -> Positive"
    if kind in ("reaffirm", "other") and "revised from" in (outlook or "").lower():
        o_now = _outlook_of((outlook or "").lower().split("revised from")[0])
        if o_now == "Negative":
            kind, derived = "outlook_negative", f"outlook text: {outlook}"
        elif o_now == "Positive":
            kind, derived = "outlook_positive", f"outlook text: {outlook}"
    return kind, kind in ADVERSE, derived


def _agency_key(name: str) -> str:
    low = name.lower()
    for k in ("crisil", "icra", "care", "india ratings", "brickwork", "acuite", "infomerics"):
        if k in low:
            return k
    return low.strip()


def parse_credit_ratings(data: Any) -> list[RatingAction]:
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise NseError(f"credit-rating payload is not a list: {type(data).__name__}")
    out = []
    for r in rows:
        rating, outlook_raw = _txt(r.get("CreditRating")), _txt(r.get("Outlook"))
        agency, e_agency = _txt(r.get("NameOfCRAgency")), _txt(r.get("NameOfCRAgencyEarlier"))
        e_rating, e_outlook = _txt(r.get("CreditRatingEarlier")), _txt(r.get("OutlookEarlier"))
        act, oth = _txt(r.get("RatingAction")), _txt(r.get("SpecifyOthRatingActn"))
        kind, adverse, derived = classify_rating(
            act, oth, rating, outlook_raw, agency, e_rating, e_outlook, e_agency
        )
        sym = str(r.get("Symbol") or "").strip().upper()
        out.append(RatingAction(isin=_isin(r.get("ISIN")), symbol=sym if sym and sym not in ("NOTLISTED", "NA", "-") else None,
                                company=_txt(r.get("CompanyName")), agency=agency, rating=rating,
                                outlook=_outlook_of(outlook_raw, rating), watch=_watch_of(rating, outlook_raw),
                                action_raw=" / ".join(x for x in (act, oth) if x) or None, action=kind, adverse=adverse,
                                rating_day=parse_day(r.get("DateofCR")), broadcast_at=parse_when(r.get("BroadcastDateTime")),
                                earlier_agency=e_agency, earlier_rating=e_rating, earlier_outlook=e_outlook,
                                derived=derived))  # fmt: skip
    return out


def issuer_code(isin: str | None) -> str | None:
    """The issuer part of an Indian ISIN: "INE511C07AC7" -> "INE511C". NSDL's ISIN format: "IN", an issuer-type
    character ("E" companies), a 4-character issuer code, a 2-character security type ("01" equity; NCDs and bonds
    "07"/"08"; commercial paper "14"), a 2-character serial and a check digit [NSDL ISIN structure, unverified
    this session]. Checked on the 01-Oct-2026 rating list: one issuer's NCDs, sub-debt and equity share the prefix."""
    s = _isin(isin)
    return s[:7] if s and s[2] == "E" else None


# --------------------------------------------------------------------------- the client
class Feed(BaseModel):
    """A parsed feed plus where and when it came from."""

    model_config = {"arbitrary_types_allowed": True}

    url: str
    fetched_at: datetime
    items: Any


class NseDisclosures:
    """Disclosure endpoints on top of an NseClient (one cookie session, the client's rate limit)."""

    def __init__(self, client: NseClient | None = None) -> None:
        self.nse = client or NseClient(warmup_url=INSIDER_PAGE)
        self._warmed_for: str | None = None

    async def __aenter__(self) -> NseDisclosures:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.nse.aclose()

    @property
    def http(self) -> PoliteClient:
        return self.nse.http

    async def _json(self, path: str, params: dict[str, str] | None, referer: str) -> tuple[Any, Fetched]:
        """GET an /api path with the feed's own page as referer; a stale session (401/403 or a non-JSON page) is
        re-warmed once. Over-size and non-2xx answers raise (never an empty result)."""
        url = f"{NSE_BASE}{path}"
        resp = None
        for attempt in range(2):
            if self._warmed_for is None:
                self.http.cookies.clear()
                warm = await self.http.get(referer)
                if not warm.ok:
                    raise NseError(f"NSE warm-up failed: HTTP {warm.status} for {referer}")
                self._warmed_for = referer
            resp = await self.http.get(url, params=params, headers={**API_HEADERS, "Referer": referer})
            stale = resp.status in (401, 403) or (resp.ok and not _looks_json(resp))
            if stale and attempt == 0:
                self._warmed_for = None
                continue
            break
        assert resp is not None
        if resp.status in (401, 403) or (resp.ok and not _looks_json(resp)):
            raise NseError(f"NSE refused {path} after re-warm: HTTP {resp.status} (non-JSON or block page)")
        if not resp.ok:
            raise NseError(f"NSE HTTP {resp.status} for {path}")
        if resp.record.size > MAX_JSON_BYTES:
            raise NseError(
                f"NSE answer for {path} is {resp.record.size} bytes, over the {MAX_JSON_BYTES} cap"
            )
        try:
            return resp.json(), resp
        except ValueError as e:
            raise NseError(f"NSE returned invalid JSON for {path}") from e

    @staticmethod
    def _feed(resp: Fetched, items: Any) -> Feed:
        return Feed(url=resp.record.url, fetched_at=resp.record.fetched_at, items=items)

    # ---- market-wide lists (one request each, once a day)
    async def asm(self) -> Feed:
        data, resp = await self._json("/api/reportASM", None, SURVEILLANCE_PAGE)
        return self._feed(resp, parse_asm(data))

    async def gsm(self) -> Feed:
        data, resp = await self._json("/api/reportGSM", None, SURVEILLANCE_PAGE)
        return self._feed(resp, parse_gsm(data))

    async def credit_ratings(self) -> Feed:
        data, resp = await self._json("/api/corporate-credit-rating", {"index": "equities"}, RATING_PAGE)
        return self._feed(resp, parse_credit_ratings(data))

    async def fno_ban(self) -> Feed:
        """The F&O ban list for the next trade date. Cached for 30 minutes, and only when the body carries the ban
        header (a block page or the site's HTML shell is never cached)."""
        resp = await self.http.get(FNO_BAN_URL, headers={"Referer": f"{NSE_BASE}/"}, cache_ttl=1800.0,
                                   cache_if=lambda r: bool(_BAN_HEAD.search(r.text[:500])))  # fmt: skip
        if not resp.ok:
            raise NseError(f"NSE HTTP {resp.status} for {FNO_BAN_URL}")
        if resp.record.size > MAX_CSV_BYTES:
            raise NseError(f"F&O ban file is {resp.record.size} bytes, over the {MAX_CSV_BYTES} cap")
        return self._feed(resp, parse_fno_ban(resp.text))

    # ---- per stock
    async def pledge(self, symbol: str) -> Feed:
        data, resp = await self._json(
            "/api/corporate-pledgedata", {"index": "equities", "symbol": symbol}, PLEDGE_PAGE
        )
        return self._feed(resp, parse_pledge(data, symbol))

    async def sast29(self, symbol: str) -> Feed:
        data, resp = await self._json(
            "/api/corporate-sast-reg29", {"index": "equities", "symbol": symbol}, SAST_PAGE
        )
        return self._feed(resp, parse_sast(data))

    async def pit_index(self, symbol: str, start: date, end: date) -> Feed:
        params = {"index": "equities", "symbol": symbol, "from_date": start.strftime("%d-%m-%Y"),
                  "to_date": end.strftime("%d-%m-%Y")}  # fmt: skip
        data, resp = await self._json("/api/corporates-pit-gg", params, INSIDER_PAGE)
        return self._feed(resp, parse_pit_index(data))

    async def pit_filing(self, url: str) -> list[InsiderTxn]:
        """One filing's XBRL, from NSE's archive (cached on disk: filings never change)."""
        if not url.startswith(f"{ARCHIVES}/"):
            raise NseError(f"refusing a PIT XBRL outside NSE's archive: {url}")
        resp = await self.http.get(url, headers={"Referer": f"{NSE_BASE}/"}, cache_ttl=XBRL_CACHE_S,
                                   cache_if=lambda r: b"<xbrli:xbrl" in r.content[:4000])  # fmt: skip
        if not resp.ok:
            raise NseError(f"NSE HTTP {resp.status} for {url}")
        if resp.record.size > MAX_XBRL_BYTES:
            raise NseError(f"PIT XBRL {url} is {resp.record.size} bytes, over the {MAX_XBRL_BYTES} cap")
        if b"<xbrli:xbrl" not in resp.content[:4000]:
            raise NseError(f"PIT XBRL {url} is not an XBRL instance (a block page?)")
        return parse_pit_xbrl(resp.content)

    async def deals(self, symbol: str, kind: str, start: date, end: date) -> Feed:
        if kind not in ("bulk", "block"):
            raise ValueError("kind must be bulk or block")
        params = {"optionType": f"{kind}_deals", "symbol": symbol, "from": start.strftime("%d-%m-%Y"),
                  "to": end.strftime("%d-%m-%Y")}  # fmt: skip
        data, resp = await self._json("/api/historicalOR/bulk-block-short-deals", params, DEALS_PAGE)
        return self._feed(resp, parse_deals(data, kind))


def _looks_json(resp: Fetched) -> bool:
    head = resp.content.lstrip()[:1]
    return head in (b"{", b"[")


def window(end: date, days: int) -> tuple[date, date]:
    """(start, end) of a trailing window of `days` calendar days including `end`."""
    return end - timedelta(days=days - 1), end

"""The income-tax AIS (Annual Information Statement; Form 168 under the Income-tax Act 2025) -> the few rows the
AIS check needs, with every identifier dropped.

What is verified (research notes 5-Oct-2026):
* The AIS downloads as PDF, JSON or CSV (portal FAQ Q-9, https://www.incometax.gov.in/iec/foportal/ais-faq) [V].
* Part A holds PAN, masked Aadhaar, name, date of birth, mobile, e-mail and address; Part B holds TDS/TCS, SFT
  (Statement of Financial Transactions), tax payments, demand/refund and "other" information, each with an
  information code, description, source and value (FAQ Q-2) [V].
* The PDF is locked with the PAN in lower case followed by the date of birth as ddmmyyyy, e.g. aaaaa1234a21011991
  (help text in the AIS app bundle, https://ais.insight.gov.in main.*.js) [V].
* The same income can be reported twice (e.g. savings interest under TDS and SFT); only the "value processed by
  system" is de-duplicated (same bundle) [V]. The JSON download is labelled "JSON (for AIS Utility)" [V].

What is NOT verified ("format unverified"): the JSON layout itself. The field names below are the ones the AIS app
uses on screen (infoCode, infoDesc, infoSrc, amountReported, salesConsideration, dividendAmount, quantity,
transactionDate, taxDeducted ...; column keys SFT-Sales-Consideration, SFT-STT, SFT-Dividend-Amount,
TDS-TCS-Amount-Paid-Credited ...), recovered from that bundle, not from a downloaded file. So the parser walks any
JSON tree, takes the deepest objects that carry an amount, inherits the code/description/source from their parents and
classifies each row by its description first and its code second. The SFT code numbers (SFT-015 dividend, SFT-016
interest, SFT-017 sale and SFT-018 purchase of securities and MF units) are from secondary sources [U]; TDS 193/194A
(interest), 194 (dividend) and 194K (income from MF units) are the Income-tax Act 1961 sections, renumbered under the
2025 Act from 1-Apr-2026, which is another reason descriptions win over codes.

Privacy: only whitelisted fields are read (FY, category, code, description, source name/TAN, security name/ISIN,
date, amount, TDS, quantity, STT); Part A is never walked; names and descriptions are scrubbed of anything shaped like
a PAN, Aadhaar, account or phone number, or e-mail. The file itself is never saved (unlike a CAS upload). Nothing here
logs or touches the network.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finresearch.portfolio.importers import StatementError, _dec, parse_day

FORMAT_NOTE = ("AIS format unverified: the JSON layout is read by field names recovered from the AIS app, not from a "
               "published schema; check any row marked only-in-AIS against the AIS on the portal.")  # fmt: skip
CATEGORIES = ("dividend", "interest", "sale", "purchase", "off_market")

_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Za-z0-9])", re.I)  # also inside "AIS_<PAN>_..."
_TAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{4}\d{5}[A-Z](?![A-Za-z0-9])")
_ISIN = re.compile(r"(?<![A-Za-z0-9])IN[EF0-9][A-Z0-9]{8}\d(?![A-Za-z0-9])")
_EMAIL = re.compile(r"\S+@\S+")
_MASKED = re.compile(r"(?<![A-Za-z])[Xx*]{3,}[\dXx* -]*\d")  # XXXXXX1234, XXXX XXXX 1234
_DIGITS = re.compile(r"\d[\d -]{4,}\d")  # account, Aadhaar, mobile, folio numbers (6+ digits)


def scrub(text: Any) -> str | None:
    """A name or description with anything identifying removed (the source's TAN and an ISIN are kept)."""
    if text is None:
        return None
    t = str(text)
    keep = {m.group(0): f"\x00{i}\x00" for i, m in enumerate([*_TAN.finditer(t), *_ISIN.finditer(t)])}
    for k, v in keep.items():
        t = t.replace(k, v)
    for rx in (_EMAIL, _PAN, _MASKED, _DIGITS):
        t = rx.sub(" ", t)
    for k, v in keep.items():
        t = t.replace(v, k)
    t = re.sub(r"\(\s*\)|\[\s*\]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -,:;/")
    return t or None


def scrub_filename(name: str) -> str:
    """A file name safe to log: a PAN-shaped token or a long number (account, Aadhaar, phone) becomes "x"."""
    return re.sub(r"\d{9,}", "x", _PAN.sub("x", name))[:200]


@dataclass
class AisItem:
    category: str  # dividend | interest | sale | purchase | off_market
    part: str  # tds | sft | other
    amount: Decimal
    code: str | None = None
    description: str | None = None
    source: str | None = None  # payer or reporting entity
    tan: str | None = None
    security: str | None = None
    isin: str | None = None
    day: date | None = None
    tds: Decimal | None = None
    quantity: Decimal | None = None
    stt: Decimal | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("amount", "tds", "quantity", "stt"):
            d[k] = None if d[k] is None else format(d[k], "f")
        d["day"] = self.day.isoformat() if self.day else None
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> AisItem:
        num = {k: _dec(d.get(k)) for k in ("tds", "quantity", "stt")}
        return cls(**{**d, **num, "amount": _dec(d.get("amount")) or Decimal(0),
                      "day": date.fromisoformat(d["day"]) if d.get("day") else None})  # fmt: skip


@dataclass
class AisStatement:
    fy: int | None  # named by its end year (fincalc.dates.fiscal_year)
    items: list[AisItem]
    format: str  # json | pdf
    ignored: int = 0  # rows in categories this check does not use (salary, rent, cash deposits ...)
    # of `ignored`: rows the parser could not place at all (no AIS-shaped information code and a description it
    # does not classify). Rows with a code such as SFT-005 or 192 are understood, just not compared.
    unrecognised: int = 0
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- classification
_CODE_CATEGORY = {"SFT-015": "dividend", "SFT-016": "interest", "SFT-017": "sale", "SFT-018": "purchase",  # [U]
                  "194": "dividend", "194K": "dividend", "193": "interest", "194A": "interest"}  # fmt: skip


def _code(v: Any) -> str | None:
    """'SFT-017', 'TDS-194A' -> '194A', 'Section 194' -> '194'."""
    if v is None:
        return None
    t = re.sub(r"\s+", "", str(v)).upper()
    if m := re.fullmatch(r"SFT-?(\d{3}).*", t):
        return f"SFT-{m.group(1)}"
    if m := re.fullmatch(r"(?:TDS-?|SECTION|SEC\.?|U/S)?(\d{3}[A-Z]{0,3})", t):
        return m.group(1)
    return t[:20] or None


def classify(code: str | None, description: str | None) -> str | None:
    """The AIS check's category for a row, by description keywords first (they survive renumbering), then code."""
    d = f" {(description or '').lower()} "
    if "off market" in d or "off-market" in d:
        return "off_market"
    if "dividend" in d or "income in respect of units" in d or "income from units" in d:
        return "dividend"
    if "interest" in d:
        return "interest"
    if re.search(r"\bsale\b|\bsold\b|redemption|\bsell\b", d):
        return "sale"
    if re.search(r"\bpurchase\b|\bbought\b", d):
        return "purchase"
    return _CODE_CATEGORY.get(code or "")


# --------------------------------------------------------------------------- JSON
def _k(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# Normalised keys (lower case, alphanumerics only) -> the field they fill. Amounts: the first one present wins.
_AMOUNT_KEYS = ("salesconsideration", "sftsalesconsideration", "saleconsideration", "dividendamount",
                "sftdividendamount", "interestamount", "totalpurchaseamount", "sfttotalpurchaseamount",
                "purchaseamount", "amountpaidcredited", "tdstcsamountpaidcredited", "amtpaid", "amountpaid",
                "grossamount", "transactionamount", "sfttransactionamount", "amountreported", "reportedvalue",
                "amount", "value")  # fmt: skip
_FIELDS = {
    "code": ("infocode", "informationcode", "sftcode", "code", "section", "tdssection"),
    "description": ("infodesc", "informationdescription", "infodescription", "description", "desc", "infocat",
                    "informationcategory", "transactiontype", "sfttransactiontype"),
    "source": ("infosrc", "informationsource", "sourcename", "source", "nameofdeductor", "deductorname",
               "reportingentity", "filername", "amcname", "sftamcnamecode", "companyname"),
    "tan": ("tan", "deductortan", "tanofdeductor"),
    "security": ("securityname", "scripname", "schemename", "nameofsecurity", "securitydescription", "scrip"),
    "isin": ("isin", "isincode"),
    "day": ("transactiondate", "sfttransactiondate", "dateofpaymentcredit", "tdstcsdateofpaymentcredit",
            "dateoftransfer", "sftdateoftransfer", "transdate", "date"),
    "tds": ("taxdeducted", "tdsdeducted", "tdstcstdsdeducted", "amountdeducted"),
    "quantity": ("quantity", "sftquantity", "transferquantity", "units", "noofunits"),
    "stt": ("stt", "sftstt"),
}  # fmt: skip
_INHERIT = ("code", "description", "source", "tan", "security", "isin", "day")
# Subtrees never read: Part A (identity), tax payments, demand/refund, feedback history.
_SKIP = ("generalinfo", "parta", "taxpayerinfo", "personal", "profile", "address", "contact", "feedback", "activity",
         "history", "demand", "refund", "taxpayment", "paymentoftaxes", "selfassessment", "advancetax")  # fmt: skip
_FY_KEYS = ("financialyear", "finyear", "fy", "assessmentyear", "ay")


def _part(key: str) -> str | None:
    if "sft" in key:
        return "sft"
    if "tds" in key or "tcs" in key:
        return "tds"
    if key.startswith("other"):
        return "other"
    return None


def _pick(flat: dict[str, Any], names: tuple[str, ...]) -> Any:
    for n in names:
        v = flat.get(n)
        if v not in (None, "", "-"):
            return v
    return None


def _walk(node: Any, ctx: dict[str, Any], part: str, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(node, list):
        for x in node:
            _walk(x, ctx, part, out, depth + 1)
        return
    if not isinstance(node, dict):
        return
    flat = {_k(k): v for k, v in node.items() if not isinstance(v, dict | list)}
    here = {f: v for f in _FIELDS if (v := _pick(flat, _FIELDS[f])) is not None}
    if (
        "description" in here
        and ctx.get("description")
        and str(here["description"]) not in str(ctx["description"])
    ):
        here["description"] = (
            f"{ctx['description']} / {here['description']}"  # keep the parent's category words
        )
    ctx = {**ctx, **{f: v for f, v in here.items() if f in _INHERIT}}
    before = len(out)
    for key, v in node.items():
        if isinstance(v, dict | list):
            nk = _k(key)
            if any(s in nk for s in _SKIP):
                continue
            _walk(v, ctx, _part(nk) or part, out, depth + 1)
    amount = _pick(flat, _AMOUNT_KEYS)
    if len(out) == before and amount is not None and _dec(amount) is not None:
        out.append({**ctx, **here, "amount": amount, "part": part})


def _fy_from(v: Any, assessment: bool) -> int | None:
    """'2025-26', '2025-2026', 'FY 2025-26' -> 2026 (named by the end year); an assessment year is one later."""
    v = " ".join(
        str(v).split()
    )  # whitespace collapsed first, so the patterns below are linear (py/polynomial-redos)
    m = re.search(r"(20\d\d) ?[-/] ?(\d{2,4})", v)
    if m:
        end = int(m.group(1)) + 1
    elif re.fullmatch(r"20\d\d", v):
        end = int(v)  # a bare year is taken as the end year
    else:
        return None
    return end - 1 if assessment else end


def _find_fy(node: Any, depth: int = 0) -> int | None:
    if depth > 3 or not isinstance(node, dict):
        return None
    for key, v in node.items():
        nk = _k(key)
        if (nk in _FY_KEYS and not isinstance(v, dict | list)
                and (fy := _fy_from(v, nk in ("assessmentyear", "ay"))) is not None):  # fmt: skip
            return fy
    for v in node.values():
        if isinstance(v, dict) and (fy := _find_fy(v, depth + 1)) is not None:
            return fy
    return None


AIS_FILENAME = re.compile(r"(^|[^a-z])ais([^a-z]|$)|annual.?information", re.I)  # the drop zone uses the same


def looks_like_ais_json(content: bytes) -> bool:
    """A cheap check for routing a dropped .json file: it parses and mentions AIS-shaped keys."""
    head = content[:200_000].lower()
    if not head.lstrip().startswith((b"{", b"[")):
        return False
    return any(k in head for k in (b"infocode", b"sft", b"tdstcs", b"annual information", b'"ais'))


def _item(raw: dict[str, Any]) -> AisItem | None:
    code = _code(raw.get("code"))
    desc = scrub(raw.get("description"))
    cat = classify(code, desc)
    amount = _dec(raw.get("amount"))
    if cat is None or amount is None:
        return None
    src_raw = str(raw.get("source") or "")
    tan = str(raw.get("tan") or "").strip().upper() or (m.group(0) if (m := _TAN.search(src_raw)) else None)
    sec_raw = str(raw.get("security") or "")
    isin = str(raw.get("isin") or "").strip().upper() or (
        m.group(0) if (m := _ISIN.search(sec_raw)) else None
    )
    part = raw.get("part") or ("sft" if (code or "").startswith("SFT") else "tds" if code else "other")
    return AisItem(category=cat, part=part, amount=amount, code=code, description=(desc or "")[:200] or None,
                   source=(scrub(_TAN.sub(" ", src_raw)) or "")[:200] or None, tan=tan if tan and _TAN.fullmatch(tan) else None,
                   security=(scrub(sec_raw) or "")[:200] or None, isin=isin if isin and _ISIN.fullmatch(isin) else None,
                   day=parse_day(raw.get("day")) if raw.get("day") else None, tds=_dec(raw.get("tds")),
                   quantity=_dec(raw.get("quantity")), stt=_dec(raw.get("stt")))  # fmt: skip


# an AIS information code after _code(): SFT-015, 194A (a TDS section), TCS-206C
_KNOWN_CODE = re.compile(r"SFT-\d{3}|TCS-?[0-9A-Z]{2,6}|\d{3}[A-Z]{0,3}")


def statement_from_records(records: list[dict[str, Any]], fy: int | None, fmt: str) -> AisStatement:
    items, ignored, unrecognised = [], 0, 0
    for raw in records:
        it = _item(raw)
        if it is None:
            ignored += 1
            if not _KNOWN_CODE.fullmatch(_code(raw.get("code")) or ""):
                unrecognised += 1
        else:
            items.append(it)
    if fy is None:
        days = [i.day for i in items if i.day]
        fys = {d.year + 1 if d.month >= 4 else d.year for d in days}
        fy = fys.pop() if len(fys) == 1 else None
    warnings = [FORMAT_NOTE]
    if fy is None:
        warnings.append("The financial year could not be read from the file: choose it when importing.")
    return AisStatement(
        fy=fy, items=items, format=fmt, ignored=ignored, unrecognised=unrecognised, warnings=warnings
    )


def parse_ais_json(content: bytes) -> AisStatement:
    """Parse an AIS JSON download. Raises StatementError with a safe message (never quoting the file)."""
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise StatementError("this file is not plain JSON. If it is the AIS JSON for the AIS Utility it may be "
                             "encoded for that utility: import the AIS PDF instead") from None  # fmt: skip
    if not isinstance(data, dict | list):
        raise StatementError("this JSON is not an AIS statement")
    records: list[dict[str, Any]] = []
    _walk(data, {}, "other", records)
    st = statement_from_records(records, _find_fy(data), "json")
    if not st.items and not st.ignored:
        raise StatementError("no AIS information rows were found in this JSON (format unverified: the layout may "
                             "have changed)")  # fmt: skip
    return st

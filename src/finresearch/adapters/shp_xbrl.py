"""Shareholder categories from the XBRL of a shareholding-pattern filing (SEBI LODR Reg 31, BSE 'in-bse-shp' taxonomy).

NSE's shareholding summary (`corporate-share-holdings-master`) gives only promoter / public / employee trusts, but every
row links the company's filed XBRL, which carries the full statement: Institutions (Domestic) - mutual funds,
insurers, banks, pension funds, AIFs...; Institutions (Foreign) - FPI category I and II...; Non-institutions -
resident individuals up to / above Rs 2 lakh of nominal capital, NRIs, bodies corporate...

Facts are read from the contexts whose only dimension is `CategoryOfShareholdersAxis` (the category rows of the
statement); the named-holder detail rows (typed dimensions) are ignored. Three taxonomy vintages exist and are all
handled by keying on the axis member, never on the context id:
- 2020-09-30 (filings up to ~Mar-2022): one "Institutions" block; FPIs sit inside it; overseas depositories are public.
- 2022-09-30: Institutions (Domestic) / (Foreign), FPI category I/II; percentages as percents (13.82).
- 2025-05-31 and later: same categories; percentages as fractions (0.1382) - normalised here to percents.

Percentages are the filed "shareholding as a % of total no. of shares (calculated as per SCRR, 1957)", i.e. of
(A+B+C2): shares underlying depository receipts (C1, e.g. ADRs) are outside that basis and are reported separately.
"""

from __future__ import annotations

import contextlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
PCT_FACT = "ShareholdingAsAPercentageOfTotalNumberOfShares"
SHARES_FACT = "NumberOfShares"
HOLDERS_FACT = "NumberOfShareholders"
TOLERANCE = Decimal("0.05")  # filed percentages are rounded to 2 dp; a remainder within this of zero is zero

# normalised axis member (lower case, no "Member", typos fixed) -> field
_MEMBERS: dict[str, str] = {
    "shareholdingpattern": "total",
    "shareholdingofpromoterandpromotergroup": "promoter",
    "publicshareholding": "public",
    "employeebenefitstrusts": "employee_trusts",
    "custodianordrholder": "dr_holder",
    "institutionsdomestic": "institutions_domestic",
    "institutionsforeign": "institutions_foreign",
    "institutions": "institutions",  # 2020 vintage: domestic and foreign together
    "noninstitutions": "non_institutions",
    "institutionsforeignportfolioinvestorcategoryone": "fpi_1",
    "institutionsforeignportfolioinvestorcategorytwo": "fpi_2",
    "institutionsforeignportfolioinvestor": "fpi_old",
    "foreignventurecapitalinvestors": "fvci",
    "foreigndirectinvestment": "fdi",
    "mutualfundsoruti": "mutual_funds",
    "insurancecompanies": "insurance",
    "banks": "banks",
    "financialinstitutionorbanks": "banks_fis",
    "otherfinancialinstitutions": "other_fis",
    "residentindividualshareholdersholdingnominalsharecapitaluptorstwolakh": "retail",
    "individualshareholdersholdingnominalsharecapitaluptorstwolakh": "retail",
    "residentindividualshareholdersholdingnominalsharecapitalinexcessofrstwolakh": "hni",
    "individualshareholdersholdingnominalsharecapitalinexcessofrstwolakh": "hni",
    "nonresidentindians": "nri",
    "bodiescorporate": "bodies_corporate",
    "overseasdepositories": "depositories",
}

# the split the API returns, in display order: key, label, group
CATEGORIES: list[tuple[str, str, str]] = [
    ("promoter", "Promoter & group", "promoter"),
    ("fpi", "Foreign portfolio investors (FPI)", "fii"),
    ("foreign_other", "Other foreign institutions", "fii"),
    ("mutual_funds", "Mutual funds", "dii"),
    ("insurance", "Insurance companies", "dii"),
    ("banks", "Banks & financial institutions", "dii"),
    ("other_dii", "Other domestic institutions", "dii"),
    ("retail", "Retail individuals (up to ₹2 lakh)", "retail"),
    ("hni", "HNI individuals (above ₹2 lakh)", "retail"),
    ("nri", "Non-resident Indians", "retail"),
    ("bodies_corporate", "Bodies corporate", "other"),
    ("fdi", "Strategic foreign holders (FDI)", "other"),
    ("depositories", "Overseas depositories (ADR / GDR)", "other"),
    ("others", "Other public (govt, IEPF, directors...)", "other"),
    ("employee_trusts", "Employee benefit trusts", "other"),
]
GROUPS: list[tuple[str, str]] = [
    ("promoter", "Promoter"),
    ("fii", "FII / FPI"),  # portfolio and other foreign institutions; strategic FDI stakes count as "other"
    ("dii", "DII"),
    ("retail", "Individuals"),
    ("other", "Others"),
]


def _norm(member: str) -> str:
    local = member.rsplit(":", 1)[-1]
    if local.endswith("Member"):
        local = local[: -len("Member")]
    return local.lower().replace("catergory", "category")


@dataclass
class ShareholdingPattern:
    as_of: date | None
    symbol: str | None
    isin: str | None
    company: str | None
    taxonomy: str | None  # namespace date of the in-bse-shp taxonomy, e.g. "2025-10-31"
    raw: dict[str, Decimal] = field(default_factory=dict)  # filed % per recognised member field (percent)
    shares: dict[str, Decimal] = field(default_factory=dict)  # NumberOfShares per member field
    holders: dict[str, Decimal] = field(default_factory=dict)
    split: dict[str, Decimal | None] = field(default_factory=dict)  # CATEGORIES keys -> percent
    groups: dict[str, Decimal | None] = field(default_factory=dict)  # GROUPS keys -> percent
    dr_shares: Decimal | None = None  # shares underlying depository receipts (outside the % basis)
    total_shares: Decimal | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def dr_pct_of_total(self) -> Decimal | None:
        if self.dr_shares and self.total_shares:
            return self.dr_shares / self.total_shares * 100
        return None


def _dec(text: str | None) -> Decimal | None:
    try:
        return Decimal((text or "").strip()) if (text or "").strip() else None
    except InvalidOperation:
        return None


def parse_shareholding_xbrl(data: bytes) -> ShareholdingPattern:
    root = ET.fromstring(data)
    taxonomy = None
    # ElementTree drops xmlns declarations; the taxonomy date is in the element namespaces instead
    for el in root:
        if el.tag.startswith("{") and "/shp/" in el.tag:
            ns = el.tag[1:].split("}", 1)[0]
            taxonomy = ns.split("/shp/", 1)[1].split("/", 1)[0]
            break
    member_of: dict[str, str] = {}  # context id -> field
    for ctx in root.iter(f"{XBRLI}context"):
        explicit = list(ctx.iter(f"{XBRLDI}explicitMember"))
        if len(explicit) != 1 or next(ctx.iter(f"{XBRLDI}typedMember"), None) is not None:
            continue
        m = explicit[0]
        if (m.get("dimension") or "").rsplit(":", 1)[-1] != "CategoryOfShareholdersAxis":
            continue
        key = _MEMBERS.get(_norm(m.text or ""))
        if key:
            member_of[ctx.get("id") or ""] = key
    pat = ShareholdingPattern(as_of=None, symbol=None, isin=None, company=None, taxonomy=taxonomy)
    raw: dict[str, Decimal] = {}
    for el in root:
        name = el.tag.rsplit("}", 1)[-1]
        text = (el.text or "").strip()
        if name == "Symbol" and text:
            pat.symbol = pat.symbol or text
        elif name == "ISIN" and text:
            pat.isin = pat.isin or text
        elif name == "NameOfTheCompany" and text:
            pat.company = pat.company or text
        elif name == "DateOfReport" and text:
            with contextlib.suppress(ValueError):
                pat.as_of = date.fromisoformat(text[:10])
        key = member_of.get(el.get("contextRef") or "")
        if key is None:
            continue
        val = _dec(text)
        if val is None:
            continue
        if name == PCT_FACT:
            raw.setdefault(key, val)
        elif name == SHARES_FACT:
            pat.shares.setdefault(key, val)
        elif name == HOLDERS_FACT:
            pat.holders.setdefault(key, val)
    # 2025+ files fractions (total 1), older ones percents (total 100)
    total = raw.get("total")
    if total is None:
        total = (raw.get("promoter") or 0) + (raw.get("public") or 0) + (raw.get("employee_trusts") or 0)
    if total and total <= Decimal("1.5"):
        raw = {k: v * 100 for k, v in raw.items()}
    pat.raw = raw
    pat.dr_shares = pat.shares.get("dr_holder") or None
    pat.total_shares = pat.shares.get("total")
    _split(pat)
    return pat


def _split(pat: ShareholdingPattern) -> None:
    r = pat.raw

    def g(*keys: str) -> Decimal:
        return sum((r.get(k) or Decimal(0) for k in keys), Decimal(0))

    if "promoter" not in r and "public" not in r:
        pat.warnings.append("no category rows in the filing")
        return
    s: dict[str, Decimal | None] = {k: None for k, _, _ in CATEGORIES}
    s["promoter"] = r.get("promoter")
    if s["promoter"] is None and abs(g("public", "employee_trusts") - 100) <= TOLERANCE:
        s["promoter"] = Decimal(0)  # professionally managed companies (HDFC Bank, ITC) file no promoter rows
    s["employee_trusts"] = r.get("employee_trusts")
    s["mutual_funds"] = r.get("mutual_funds")
    s["insurance"] = r.get("insurance")
    s["banks"] = (
        g("banks", "banks_fis", "other_fis") if {"banks", "banks_fis", "other_fis"} & r.keys() else None
    )
    if "institutions_foreign" in r or "institutions_domestic" in r:  # 2022+ statement
        s["fpi"] = g("fpi_1", "fpi_2")
        s["fdi"] = r.get("fdi")
        s["foreign_other"] = _nonneg(g("institutions_foreign") - s["fpi"] - g("fdi"), pat, "foreign_other")
        dii = g("institutions_domestic")
    else:  # 2020 statement: FPIs and foreign VC investors inside "Institutions"
        s["fpi"] = r.get("fpi_old")
        s["foreign_other"] = r.get("fvci")
        dii = g("institutions") - g("fpi_old", "fvci")
        s["depositories"] = r.get(
            "depositories"
        )  # a public non-institution before 2022; later outside the basis
    s["other_dii"] = _nonneg(
        dii - g("mutual_funds", "insurance", "banks", "banks_fis", "other_fis"), pat, "other_dii"
    )
    s["retail"] = r.get("retail")
    s["hni"] = r.get("hni")
    s["nri"] = r.get("nri")
    s["bodies_corporate"] = r.get("bodies_corporate")
    public_named = sum((s[k] or Decimal(0) for k in ("fpi", "foreign_other", "mutual_funds", "insurance", "banks",
                                                      "other_dii", "retail", "hni", "nri", "bodies_corporate",
                                                      "fdi", "depositories")),
                       Decimal(0))  # fmt: skip
    if "public" in r:
        s["others"] = _nonneg(r["public"] - public_named, pat, "others")
    pat.split = s
    groups: dict[str, Decimal | None] = {}
    for gk, _ in GROUPS:
        vals = [s[k] for k, _, grp in CATEGORIES if grp == gk]
        groups[gk] = (
            sum((v for v in vals if v is not None), Decimal(0)) if any(v is not None for v in vals) else None
        )
    pat.groups = groups
    total = sum((v for v in groups.values() if v is not None), Decimal(0))
    if abs(total - 100) > Decimal("0.2"):
        pat.warnings.append(f"categories add up to {total:.2f}%, not 100%")


def _nonneg(v: Decimal, pat: ShareholdingPattern, name: str) -> Decimal:
    if v < 0:
        if v < -TOLERANCE:
            pat.warnings.append(f"{name} computed negative ({v:.2f}%); shown as 0")
        return Decimal(0)
    return v

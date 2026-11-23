"""Accounting-identity, recomputation and unit/scale checks over a run's claim ledger (no LLM).

Research basis: docs/dev/RESEARCH_ROADMAP.md §C.1 (accounting identities), §C.2 (unit and scale detection) and
§C.3 (restated vs audited vs later filings). The XBRL US DQC rules (e.g. DQC_0004 A = L + E) are a design
template only; these checks are written for Ind AS statements as agents record them in the ledger.

`check_claims` is a pure function over plain claim dicts (the API / replay-fixture shape: id, metric, value,
unit, period, status, importance, stream, statement, corrects_claim_id, citations), so the same code serves the
pipeline (`run_identities`, after the streams and before synthesis), the publish gate (`verify.gate`), the report
reader (`api.insights`) and the `identity_checks` MCP tool.

Checks (each result names the claims it used):

* P&L subtotals — revenue + other income = total income; total income − total expenses (± exceptional items) = PBT;
  PBT − tax = PAT; PAT = owners' share + NCI.
* EPS identity — basic EPS × weighted-average shares ≈ PAT attributable to owners (1 %, Ind AS 33).
* EPS share basis — PAT ÷ EPS gives the share count behind each period's EPS. After a bonus issue or split every
  period must be restated (Ind AS 33 ¶64), so the implied counts must agree; a period whose count is ≈ 1/k of the
  others is an un-restated EPS (k = 1 + bonus ratio or the split factor).
* Balance sheet — total assets = total equity (+ NCI) + total liabilities (or current + non-current liabilities).
* Cash roll-forward — opening cash + CFO + CFI + CFF (+ FX) = closing cash (opening = previous year's closing
  when only that is in the ledger).
* Margins and growth recomputed from their parts (EBITDA / PAT margin; YoY growth; CAGR).
* Scale shift (C.2) — two ledger figures for the same metric, period, basis and currency that differ by
  10^n (n ∈ ±1, ±2, ±3, ±5, ±7) within 0.5 %: a lakh / crore / million / thousand mix-up (million↔crore is 10×).
  The same printed number labelled ₹ in one claim and US$ in another is a currency-label mix-up.
* Basis mix — a figure with no basis label that equals the standalone figure while a different consolidated
  figure exists for the same metric and period.
* Cross-document (C.3) — the same metric, period and basis cited from different documents with different values
  (RHP restated vs annual report, or a later filing): must be explained by the restatement-adjustments note.
* Sanity bands (C.2) — PAT margin within −200 %…80 %, P/E within 0…500, receivable days within 0…730.

Tolerance (roadmap C.1): ε = max(0.5 × reporting unit × lines summed, 0.1 % of the largest operand), where the
reporting unit is the last printed decimal of the coarsest operand. Rates (margins, growth) pass within half a unit
of the printed percentage plus 0.5 % relative. A formula with several accepted definitions (EBITDA margin on
revenue or total income; PAT or owners' PAT) passes if any definition matches, and the result records which.
Outcomes are pass / warn (small gap, or a gap exceptional items could explain) / fail; checks without inputs are
listed as "not enough inputs", never guessed. How warn / fail become publish-gate warnings or blocks is the severity
policy in `verify.gate.identity_findings`.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from finresearch.fincalc.forensic import FINANCIAL_BASES

USABLE = ("verified", "needs_review", "unverified")
REL_EPS = Decimal("0.001")  # 0.1 % floor of the identity tolerance
WARN_REL = Decimal("0.02")  # a gap up to 2 % is a warning (rounding of many lines, small unlisted items)
EPS_TOL = Decimal("0.01")  # EPS × shares vs PAT
SCALE_TOL = Decimal("0.005")
SCALE_POWERS = (1, 2, 3, 5, 7)
MAX_COMBOS = 256

# --------------------------------------------------------------------------- canonical metrics
_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue_from_operations", "revenue", "revenue_from_ops", "net_sales", "total_revenue_from_operations",
                "revenue_from_operations_consolidated", "net_revenue_from_operations", "sales"),
    "other_income": ("other_income",),
    "total_income": ("total_income",),
    "total_expenses": ("total_expenses", "total_expenditure"),
    "pbt_pre_exceptional": ("profit_before_exceptional_items_and_tax", "profit_before_exceptional_items",
                            "pbt_before_exceptional_items", "pbt_pre_exceptional"),
    "exceptional": ("exceptional_items", "exceptional_item", "exceptional_items_net"),
    "pbt": ("pbt", "profit_before_tax", "restated_pbt", "restated_profit_before_tax"),
    "tax": ("tax_expense", "total_tax_expense", "tax", "income_tax_expense", "total_tax"),
    "pat": ("pat", "profit_for_the_year", "profit_for_the_period", "profit_after_tax", "net_profit", "consolidated_pat",
            "restated_pat", "restated_profit_for_the_year", "profit_for_the_year_consolidated"),
    "pat_owners": ("profit_attributable_to_owners", "pat_attributable_to_owners", "profit_attributable_to_equity_holders",
                   "net_profit_attributable_to_owners", "pat_owners"),
    "pat_nci": ("profit_attributable_to_nci", "profit_attributable_to_non_controlling_interests", "pat_nci",
                "nci_share_of_profit"),
    "eps_basic": ("basic_eps", "eps_basic", "basic_diluted_eps", "eps", "restated_basic_eps", "reported_basic_eps"),
    "weighted_shares": ("weighted_avg_basic_shares", "weighted_average_shares", "weighted_average_number_of_shares",
                        "weighted_avg_shares", "weighted_average_basic_shares"),
    "shares_outstanding": ("shares_outstanding", "current_shares_outstanding", "pre_issue_shares",
                           "total_shares_outstanding", "equity_shares_outstanding"),
    "total_assets": ("total_assets",),
    "total_equity": ("total_equity", "net_worth", "equity_attributable_to_owners", "shareholders_equity"),
    "nci_equity": ("non_controlling_interest_equity", "nci_equity"),
    "total_liabilities": ("total_liabilities",),
    "current_liabilities": ("current_liabilities", "total_current_liabilities"),
    "non_current_liabilities": ("non_current_liabilities", "total_non_current_liabilities"),
    "cash_open": ("opening_cash", "opening_cash_and_cash_equivalents", "cash_at_beginning_of_year",
                  "cash_and_cash_equivalents_at_beginning"),
    "cash_close": ("cash_and_cash_equivalents", "cash_and_equivalents", "closing_cash",
                   "closing_cash_and_cash_equivalents", "cash_at_end_of_year", "cash_and_cash_equivalents_at_end"),
    "cfo": ("cfo", "net_cash_from_operating_activities", "cash_from_operations", "operating_cash_flow",
            "net_cash_generated_from_operating_activities", "cash_flow_from_operations"),
    "cfi": ("cfi", "net_cash_from_investing_activities", "cash_from_investing", "investing_cash_flow",
            "net_cash_used_in_investing_activities"),
    "cff": ("cff", "net_cash_from_financing_activities", "cash_from_financing", "financing_cash_flow",
            "net_cash_used_in_financing_activities"),
    "fx_cash": ("effect_of_exchange_rate_on_cash", "fx_effect_on_cash", "exchange_difference_on_cash"),
    "ebitda": ("ebitda",),
    "fcf": ("fcf", "free_cash_flow", "free_cash_flow_to_firm", "fcff"),
    "ebitda_margin": ("ebitda_margin",),
    "pat_margin": ("pat_margin", "net_profit_margin", "net_margin"),
    "pe": ("pe", "pe_ratio", "pe_basic", "trailing_pe", "trailing_pe_reported", "pe_ratio_upper_band", "pe_basic_fy26",
           "pe_post_issue_diluted"),
    "receivable_days": ("receivable_days", "trade_receivable_days", "debtor_days", "dso"),
    "bonus": ("bonus_issue", "bonus_issue_shares", "bonus_ratio", "bonus_issue_ratio"),
    "split": ("stock_split", "share_split", "split_ratio", "sub_division"),
    # Statement checks of #518. "(run N)" marks a name a live ledger used (tests/fixtures/eval); the rest are the
    # Schedule III / Ind AS 7 captions themselves, kept that narrow until a ledger shows what agents really write.
    "other_equity": ("other_equity",),
    "retained_earnings": ("retained_earnings",),
    "dividends": ("dividends_paid", "dividend_paid"),  # run 9
    "buyback": ("buyback_of_equity_shares", "buyback_amount"),  # run 9 (both)
    "esop_charge": ("esop_charge", "share_based_payment_expense"),  # run 5
    "oci": ("other_comprehensive_income",),
    "da": ("depreciation", "depreciation_and_amortisation", "depreciation_and_amortization",  # run 5: depreciation
           "depreciation_and_amortisation_expense", "depreciation_and_amortization_expense"),
    "da_cf": ("depreciation_cash_flow", "depreciation_and_amortisation_cash_flow",
              "depreciation_and_amortization_cash_flow"),
    "deferred_tax": ("deferred_tax", "deferred_tax_expense", "deferred_tax_charge", "deferred_tax_credit"),
    "mat_credit": ("mat_credit", "mat_credit_entitlement"),
    "tax_adjustment": ("tax_reversal", "tax_reversal_and_interest_footnote", "tax_for_earlier_years"),  # run 9
    "etr": ("effective_tax_rate",),  # run 9
    "gross_profit": ("gross_profit",),
    "gross_margin": ("gross_margin",),  # run 9
    "capex": ("capex", "capital_expenditure", "purchase_of_property_plant_and_equipment"),  # runs 5 and 9: capex
    "gross_block": ("gross_block", "gross_fixed_assets"),
    "cwip": ("cwip", "capital_work_in_progress"),
}  # fmt: skip
_CANON = {alias: key for key, aliases in _ALIASES.items() for alias in aliases}
_GROWTH_RE = re.compile(r"^(revenue|pat|eps|net_profit|profit)(?:_from_operations)?_(growth|cagr)(?:_(yoy|inr|usd))?"
                        r"(?:_fy\d{2,4}(?:_fy\d{2,4}|_\d{2,4})?)?$")  # fmt: skip
_GROWTH_BASE = {"revenue": "revenue", "pat": "pat", "net_profit": "pat", "profit": "pat", "eps": "eps_basic"}

# --------------------------------------------------------------------------- statement checks (#518): constants
# Effective tax rate band. Statutory rates of a domestic company under the Income-tax Act, 1961, each as
# rate × (1 + surcharge) × (1 + 4 % health and education cess); the 1961 Act governs every year up to FY2025-26
# (the Income-tax Act, 2025 applies from 1 April 2026). Rates as enacted by the Taxation Laws (Amendment) Act, 2019
# and the annual Finance Acts; not re-read from the Act's text when this was written [U].
_CESS = Decimal("1.04")
STATUTORY_TAX_RATES: tuple[tuple[str, Decimal], ...] = (
    ("s.115BAB, new manufacturing company: 15 % + 10 % surcharge + cess", Decimal("0.15") * Decimal("1.10") * _CESS),
    ("s.115JB, MAT on book profit: 15 % + 12 % surcharge + cess", Decimal("0.15") * Decimal("1.12") * _CESS),
    ("s.115BAA: 22 % + 10 % surcharge + cess", Decimal("0.22") * Decimal("1.10") * _CESS),  # 25.168 %
    ("25 % slab, turnover up to ₹400 crore (Finance Act, First Schedule): + 12 % surcharge + cess",
     Decimal("0.25") * Decimal("1.12") * _CESS),
    ("30 % slab, other domestic companies: + 12 % surcharge + cess", Decimal("0.30") * Decimal("1.12") * _CESS),
)  # fmt: skip
# Total tax (current + deferred, Ind AS 12) ÷ PBT sits near the statutory rate because deferred tax is booked at the
# enacted rate; permanent differences (exempt income, disallowed expenses, a subsidiary taxed elsewhere) move it by a
# few points. The cushion on each side of the lowest and highest statutory rate is a judgement [U].
TAX_BAND_CUSHION = Decimal("0.03")
TAX_BAND = (min(r for _, r in STATUTORY_TAX_RATES) - TAX_BAND_CUSHION,
            max(r for _, r in STATUTORY_TAX_RATES) + TAX_BAND_CUSHION)  # 14.16 % … 37.944 %  # fmt: skip
# Cash capex against the rise in gross block (+ CWIP): disposals, capital advances and creditors, leased assets
# (Ind AS 116 adds to the block without cash) and acquisitions all sit between the two, so only a gap above a
# quarter of the larger figure is worth a question. A judgement [U].
CAPEX_BAND = Decimal("0.25")
GROWTH_FLAG = Decimal(2)  # this year above 2× last year = growth above 100 % (issue #518)
# The lines whose jump a reader expects the text to explain: what the financial streams record every year.
HEADLINE = (("revenue", "revenue"), ("ebitda", "EBITDA"), ("pbt", "PBT"), ("pat", "PAT"),
            ("pat_owners", "PAT to owners"), ("eps_basic", "basic EPS"))  # fmt: skip
# A bank, NBFC or insurer has no gross margin or EBITDA. The ledger says so through its own metrics: the three
# results-taxonomy lines `fincalc.forensic` uses, the lender metrics the financial stream is asked for (NIM, credit
# cost, GS3 / NS3) and the ones bond run 12 recorded (crar_pct, gross_stage3_pct, consolidated_loan_book).
_LENDER_RE = re.compile(
    r"(?:^|_)(?:" + "|".join(FINANCIAL_BASES) + r"|net_interest_income|net_interest_margin|nim|credit_cost|"
    r"gross_stage3|net_stage3|gs3|ns3|gnpa|nnpa|gross_npa|net_npa|crar|capital_adequacy|casa|loan_book|"
    r"gross_written_premium|solvency_ratio)(?:_|$)")  # fmt: skip
_CASH_FLOW_RE = re.compile(r"cash[- ]flow statement|statement of cash flows|added back|add-back", re.I)


def canonical(metric: str | None) -> str | None:
    m = re.sub(r"[^a-z0-9]+", "_", (metric or "").lower()).strip("_")
    if not m:
        return None
    if m in _CANON:
        return _CANON[m]
    m2 = re.sub(r"_(consolidated|standalone|restated|reported)$", "", m)
    return _CANON.get(m2)


# --------------------------------------------------------------------------- periods, basis, units
_FY_TOK = r"FY\s?'?(\d{4}|\d{2})(?:\s?[-–]\s?(\d{2}))?"
_REJECT = re.compile(r"\bTTM\b|\bLTM\b|guidance|forward|\bFY\s?\d{2,4}E\b|pro ?forma|estimate|post-|scenario|"
                     r"\bH[12]\b|\b9M\b|\bcap price\b|\bfloor price\b", re.I)  # fmt: skip
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}  # fmt: skip
_QEND = {3: (0, None), 6: (1, 1), 9: (1, 2), 12: (1, 3)}  # month -> (fy offset, quarter)
_MEND = {3: 31, 6: 30, 9: 30, 12: 31}


def _yr(tok: str) -> int:
    y = int(tok)
    return y + 2000 if y < 100 else y


def _fy_year(m: re.Match) -> int:
    """FY2026, FY26, FY2025-26 / FY2024-25 (the second year is the fiscal year)."""
    if m.group(2):
        return _yr(m.group(2)) if len(m.group(2)) == 2 and len(m.group(1)) == 4 else _yr(m.group(1))
    return _yr(m.group(1))


def _date_period(text: str) -> str | None:
    """A balance-sheet date (quarter end) -> the period it closes: 31-Mar-2026 -> FY2026, 30-Jun-2026 -> Q1FY2027."""
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mo in _QEND and d == _MEND[mo]:
            off, q = _QEND[mo]
            return f"FY{y + off}" if q is None else f"Q{q}FY{y + off}"
    for m in re.finditer(r"\b(?:(\d{1,2})[-\s])?(mar|jun|sep|dec)[a-z]*[-\s']*(\d{4}|\d{2})\b", text, re.I):
        mo, y = _MONTHS[m.group(2)[:3].lower()], _yr(m.group(3))
        d = int(m.group(1)) if m.group(1) else _MEND[mo]
        if d == _MEND[mo]:
            off, q = _QEND[mo]
            return f"FY{y + off}" if q is None else f"Q{q}FY{y + off}"
    return None


def parse_period(period: str | None) -> str | None:
    """One fiscal year ("FY2026") or quarter ("Q1FY2027") from a ledger period; None for ranges, TTM, estimates,
    market dates and anything unclear."""
    p = period or ""
    if not p.strip() or _REJECT.search(p) or re.search(r"\bvs\b", p, re.I):
        return None
    q = re.search(rf"\bQ([1-4])\s?{_FY_TOK}", p, re.I)
    if q:
        y = _yr(q.group(2))
        return f"Q{q.group(1)}FY{y}"
    if re.search(r"\bQ[1-4]\b", p):
        return None  # "FY2025 Q1" style is ambiguous
    fys = list(re.finditer(rf"\b{_FY_TOK}\b", p, re.I))
    if len(fys) > 1:
        return None
    if re.search(rf"{_FY_TOK}\s*(?:-|–|to)\s*FY", p, re.I):
        return None
    if len(fys) == 1:
        return f"FY{_fy_year(fys[0])}"
    return _date_period(p)


def _prev(period: str) -> str | None:
    m = re.fullmatch(r"(Q[1-4])?FY(\d{4})", period)
    if not m:
        return None
    return f"{m.group(1) or ''}FY{int(m.group(2)) - 1}"


def parse_growth_period(period: str | None, metric: str) -> tuple[str, str, int] | None:
    """(start, end, years) for a growth claim: "FY2026 vs FY2025" / metric "..._fy26" (YoY), "FY2024-FY2026" (CAGR)."""
    p = period or ""
    fys = [f"FY{_fy_year(m)}" for m in re.finditer(rf"\b{_FY_TOK}\b", p, re.I)]
    if len(fys) == 2:
        a, b = sorted(fys)
        return a, b, int(b[2:]) - int(a[2:])
    mm = re.findall(r"fy(\d{2,4})", metric.lower())
    if len(mm) == 2:
        a, b = sorted(f"FY{_yr(x)}" for x in mm)
        return a, b, int(b[2:]) - int(a[2:])
    end = parse_period(p) or (f"FY{_yr(mm[0])}" if len(mm) == 1 else None)
    if end and end.startswith("FY"):
        return f"FY{int(end[2:]) - 1}", end, 1
    return None


def basis_of(c: dict[str, Any]) -> str:
    """consolidated | standalone | unknown, from the metric and period first, then the statement if unambiguous."""
    head = f"{c.get('metric') or ''} {c.get('period') or ''}".lower()
    for b in ("standalone", "consolidated"):
        if b in head:
            return b
    st = (c.get("statement") or "").lower()
    has_s, has_c = "standalone" in st, "consolidated" in st
    if has_s != has_c:
        return "standalone" if has_s else "consolidated"
    return "unknown"


_SCALE_WORDS = (("crore", Decimal(10) ** 7), (" cr", Decimal(10) ** 7), ("billion", Decimal(10) ** 9),
                (" bn", Decimal(10) ** 9), ("million", Decimal(10) ** 6), (" mn", Decimal(10) ** 6),
                ("lakh", Decimal(10) ** 5), ("thousand", Decimal(1000)), (" k", Decimal(1000)))  # fmt: skip
_USD = ("usd", "us$", "$", "dollar")
# "Rs" / "INR" as words: "years", "users", "hours" are not rupees
_INR_RE = re.compile(r"\b(?:inr|rs)\b|₹|rupee")


def unit_kind(unit: str | None) -> tuple[str, str | None, Decimal]:
    """(kind, currency, factor to base units). kind: money | per_share | pct | multiple | shares | days | other.
    Money is converted to whole rupees / dollars; percentages to fractions."""
    u = f" {(unit or '').lower().strip()} "
    cur = "USD" if any(k in u for k in _USD) else ("INR" if _INR_RE.search(u) or
                                                      any(w in u for w, _ in _SCALE_WORDS[:8]) else None)  # fmt: skip
    if "per share" in u or "/share" in u or "/sh" in u:
        return "per_share", cur or "INR", Decimal(1)
    if "per " in u or "/" in u.replace("us$", ""):
        return "other", cur, Decimal(1)  # per month, per lot, per unit ...
    if "%" in u or "percent" in u or "pct" in u:
        return "pct", None, Decimal("0.01")
    if u.strip() in ("fraction", "ratio of 1"):
        return "pct", None, Decimal(1)
    if u.strip() in ("x", "times", "multiple"):
        return "multiple", None, Decimal(1)
    if u.strip().startswith("share") or u.strip() in ("equity shares", "no. of shares"):
        return "shares", None, Decimal(1)
    if "day" in u:
        return "days", None, Decimal(1)
    if cur:
        scale = next((s for w, s in _SCALE_WORDS if w in u), Decimal(1))
        return "money", cur, scale
    return "other", None, Decimal(1)


# --------------------------------------------------------------------------- facts
@dataclass(frozen=True)
class Fact:
    id: int
    key: str  # canonical metric
    metric: str
    raw: Decimal  # value as recorded
    value: Decimal  # in base units (rupees / dollars / fraction / count)
    unit: str | None
    kind: str
    currency: str | None
    period: str | None  # parsed period
    period_raw: str | None
    basis: str
    status: str
    importance: str
    ulp: Decimal  # half of the last printed digit, in base units
    docs: frozenset[str]

    def brief(self) -> dict[str, Any]:
        return {"claim_id": self.id, "metric": self.metric, "value": f"{self.raw.normalize():f}", "unit": self.unit,
                "period": self.period_raw, "status": self.status, "importance": self.importance}  # fmt: skip


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _ulp(raw: Decimal, factor: Decimal) -> Decimal:
    exp = raw.normalize().as_tuple().exponent
    dp = max(0, -int(exp)) if isinstance(exp, int) else 0
    return Decimal(1).scaleb(-dp) * factor / 2


def _docs(c: dict[str, Any]) -> frozenset[str]:
    out = set()
    for ct in c.get("citations") or []:
        d = ct.get("document_title") or ct.get("document_id") or ct.get("url")
        if d:
            out.add(str(d))
    return frozenset(out)


def facts_from(claims: Iterable[dict[str, Any]]) -> tuple[list[Fact], list[dict[str, Any]]]:
    """Usable numeric claims (verified / needs review / unverified, not superseded by a correction) as facts."""
    rows = list(claims)
    superseded = {
        c.get("corrects_claim_id") for c in rows if c.get("corrects_claim_id") and c.get("status") in USABLE
    }
    out: list[Fact] = []
    usable = []
    for c in rows:
        if c.get("status") not in USABLE or c.get("id") in superseded:
            continue
        usable.append(c)
        raw = _dec(c.get("value"))
        if raw is None:
            continue
        metric = (c.get("metric") or "").strip()
        kind, cur, factor = unit_kind(c.get("unit"))
        key = canonical(metric)
        g = _GROWTH_RE.match(re.sub(r"[^a-z0-9]+", "_", metric.lower()))
        if g and kind == "pct":
            key = f"{_GROWTH_BASE[g.group(1)]}_{g.group(2)}"
        if key == "da" and _CASH_FLOW_RE.search(c.get("statement") or ""):
            key = "da_cf"  # the add-back in the cash-flow statement, said in the claim's own words
        out.append(Fact(id=int(c["id"]), key=key or "", metric=metric, raw=raw, value=raw * factor,
                        unit=c.get("unit"), kind=kind, currency=cur, period=parse_period(c.get("period")),
                        period_raw=c.get("period"), basis=basis_of(c), status=c.get("status") or "",
                        importance=c.get("importance") or "normal", ulp=_ulp(raw, factor), docs=_docs(c)))  # fmt: skip
    return out, usable


# --------------------------------------------------------------------------- results
@dataclass
class Check:
    id: str  # stable: "<family>:<period>:<basis>"
    family: str
    title: str
    status: str  # pass | warn | fail
    period: str | None
    basis: str
    formula: str
    detail: str
    claims: list[dict[str, Any]]
    expected: float | None = None
    actual: float | None = None
    diff_pct: float | None = None
    tolerance: str | None = None
    hard: bool = False  # an unambiguous error (scale shift, un-restated EPS, a clean arithmetic break)
    hint: str | None = None

    @property
    def claim_ids(self) -> list[int]:
        return [c["claim_id"] for c in self.claims]


@dataclass
class IdentityReport:
    checks: list[Check] = field(default_factory=list)
    not_enough_inputs: list[str] = field(default_factory=list)
    facts: int = 0

    def counts(self) -> dict[str, int]:
        out = {"pass": 0, "warn": 0, "fail": 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def failing(self) -> list[Check]:
        return [c for c in self.checks if c.status != "pass"]

    def to_dict(self) -> dict[str, Any]:
        order = {"fail": 0, "warn": 1, "pass": 2}
        return {"counts": self.counts(), "applicable": len(self.checks), "facts": self.facts,
                "checks": [{**asdict(c), "claim_ids": c.claim_ids}
                           for c in sorted(self.checks, key=lambda c: (order[c.status], c.family, c.period or ""))],
                "not_enough_inputs": sorted(set(self.not_enough_inputs))}  # fmt: skip


# --------------------------------------------------------------------------- the engine
class _Ledger:
    def __init__(self, facts: list[Fact]):
        self.facts = facts
        self.by: dict[tuple[str, str], list[Fact]] = {}
        for f in facts:
            if f.key and f.period:
                self.by.setdefault((f.key, f.period), []).append(f)

    def get(self, key: str, period: str | None, basis: str, *, kind: str | None = None,
            currency: str | None = None) -> list[Fact]:  # fmt: skip
        if period is None:
            return []
        out = [f for f in self.by.get((key, period), []) if (basis == "unknown" or f.basis in (basis, "unknown"))
               and (kind is None or f.kind == kind) and (currency is None or f.currency in (currency, None))]  # fmt: skip
        # verified first; one fact per distinct value keeps the combination search small
        seen: dict[Decimal, Fact] = {}
        for f in sorted(out, key=lambda f: (f.status != "verified", f.basis == "unknown", -f.id)):
            seen.setdefault(f.value, f)
        return list(seen.values())[:6]

    def periods(self, key: str) -> set[str]:
        return {p for (k, p) in self.by if k == key}

    def bases(self, keys: Iterable[str], period: str) -> list[str]:
        found = {f.basis for k in keys for f in self.by.get((k, period), [])}
        out = [b for b in ("consolidated", "standalone") if b in found]
        return out or ["unknown"]

    def currencies(self, keys: Iterable[str], period: str) -> list[str]:
        return sorted({f.currency for k in keys for f in self.by.get((k, period), []) if f.currency}) or [
            "INR"
        ]


def _rel(a: Decimal, b: Decimal) -> Decimal:
    d = max(abs(a), abs(b))
    return abs(a - b) / d if d else Decimal(0)


def _f(x: Decimal | None) -> float | None:
    return None if x is None else float(x)


def _pct(x: Decimal) -> str:
    return f"{float(x) * 100:.2f}%"


def _amt(x: Decimal, cur: str | None) -> str:
    sym = "US$" if cur == "USD" else "₹"
    a = abs(x)
    if cur == "INR" and a >= 10**7:
        s = f"{sym}{float(x) / 1e7:,.2f} Cr"
    elif a >= 10**6:
        s = f"{sym}{float(x) / 1e6:,.2f} Mn"
    else:
        s = f"{sym}{float(x):,.2f}"
    return s


class _Engine:
    def __init__(self, facts: list[Fact]):
        self.L = _Ledger(facts)
        self.facts = facts
        self.report = IdentityReport(facts=len(facts))
        self._seen: set[tuple[str, frozenset[int]]] = set()
        self._nc: dict[tuple[str, str], list[str]] = {}

    # ---- helpers
    def add(self, chk: Check) -> None:
        sig = (chk.family, frozenset(chk.claim_ids))
        if sig in self._seen:
            return
        self._seen.add(sig)
        self.report.checks.append(chk)

    def missing(self, name: str) -> None:
        self.report.not_enough_inputs.append(name)

    def not_checked(self, check: str, what: str, where: str | None = None) -> None:
        """A statement check (#518) that could not run: which input is absent, and for which period and basis.
        One line per check and reason (`_flush_not_checked`); an absent input is never read as nil or as a pass."""
        self._nc.setdefault((check, what), [])
        if where:
            self._nc[(check, what)].append(where)

    def _flush_not_checked(self) -> None:
        for (check, what), wheres in self._nc.items():
            at = f" ({', '.join(sorted(set(wheres)))})" if wheres else ""
            self.missing(f"{check} — not checked{at}: {what}")

    @staticmethod
    def _where(period: str, basis: str) -> str:
        return period if basis == "unknown" else f"{period} {basis}"

    def _bases(self, keys: Iterable[str], periods: Iterable[str | None]) -> list[str]:
        """The labelled bases among these keys over EVERY period a cross-period check reads. `_Ledger.bases` looks at
        one period, and `get(..., "unknown")` is a wildcard: used across two periods it would pair a standalone year
        with a consolidated one. The wildcard is left only when no fact on either side carries a label."""
        found = {f.basis for k in keys for p in periods if p for f in self.L.by.get((k, p), [])}
        return [b for b in ("consolidated", "standalone") if b in found] or ["unknown"]

    def _as(self, key: str, period: str, facts: list[Fact], *, magnitude: bool = False) -> str:
        """Put `facts` in the ledger under a role name for one `sum_identity` call (as the cash roll-forward does for
        its opening balance). `magnitude`: an outflow recorded with either sign (dividends 18,653 or −18,653)."""
        self.L.by[(key, period)] = (
            [replace(f, value=abs(f.value)) for f in facts] if magnitude else list(facts)
        )
        return key

    def _lender(self) -> str | None:
        """Why the ledger reads as a bank, NBFC or insurer (naming the claims), or None. From metric names only, so
        every caller (pipeline, gate, report reader, MCP tool) gets the same answer; peers' figures do not count."""
        hits: dict[str, Fact] = {}  # one claim per metric name
        for f in self.facts:
            m = re.sub(r"[^a-z0-9]+", "_", f.metric.lower()).strip("_")
            if _LENDER_RE.search(m) and not re.search(r"(?:^|_)(?:peer|peers|industry|sector)(?:_|$)", m):
                hits.setdefault(f.metric, f)
        if not hits:
            return None
        names = [f"C{f.id} {f.metric}" for _, f in sorted(hits.items())]
        return f"the ledger holds lender or insurer metrics ({', '.join(names[:4])})"

    def sum_identity(self, family: str, title: str, period: str, basis: str, cur: str,
                     lhs: list[tuple[str, int]], rhs: str, *, soft: str | None = None,
                     signs_free: tuple[str, ...] = (), warn_only: bool = False, tag: str = "") -> bool:  # fmt: skip
        """Σ sign × lhs ≈ rhs over every candidate combination; pass if any combination is within ε.
        `signs_free`: operands whose sign convention varies in the ledger (exceptional items), tried both ways.
        `warn_only`: the statement checks of #518, which never fail and are never `hard`; `tag` keeps their ids apart."""
        cands = [self.L.get(k, period, basis, kind="money", currency=cur) for k, _ in lhs]
        tgt = self.L.get(rhs, period, basis, kind="money", currency=cur)
        if not tgt or any(not c for c in cands):
            return False
        best = None
        for combo in itertools.islice(itertools.product(*cands, tgt), MAX_COMBOS):
            *ops, t = combo
            sign_sets = [[s] if k not in signs_free else [1, -1] for (k, s) in lhs]
            for signs in itertools.product(*sign_sets):
                total = sum((Decimal(s) * f.value for s, f in zip(signs, ops, strict=True)), Decimal(0))
                eps = max(max(f.ulp for f in (*ops, t)) * (len(ops) + 1),
                          REL_EPS * max(abs(f.value) for f in (*ops, t)))  # fmt: skip
                diff = abs(total - t.value)
                cand = (diff <= eps, -diff, ops, t, total, eps, signs)
                if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                    best = cand
        ok, _, ops, t, total, eps, signs = best  # type: ignore[misc]
        rel = _rel(total, t.value)
        formula = " ".join(
            f"{'+' if s > 0 else '−'} {k}" for (k, _), s in zip(lhs, signs, strict=True)
        ).lstrip("+ ")
        formula = f"{formula} = {rhs}"
        verified = all(f.status == "verified" for f in (*ops, t))
        status = "pass" if ok else ("warn" if rel <= WARN_REL or soft else "fail")
        detail = (f"{' '.join(f'{_amt(f.value, cur)}' for f in ops)} → {_amt(total, cur)} vs stated {_amt(t.value, cur)}"
                  f" ({float(rel) * 100:.2f}% apart; tolerance {_amt(eps, cur)})")  # fmt: skip
        hint = None
        scale_slip = False
        if not ok:
            hint = self._diagnose(ops, t, total, lhs, signs)
            scale_slip = bool(hint and "unit slip" in hint)
            hint = hint or soft
            if scale_slip:
                status = "fail"  # a 10^n slip is an error however small the gap it leaves
        if warn_only and status == "fail":
            status = "warn"
        self.add(Check(id=f"{family}:{period}:{basis}:{cur}{tag}", family=family, title=title, status=status,
                       period=period, basis=basis, formula=formula, detail=detail,
                       claims=[f.brief() for f in (*ops, t)], expected=_f(total), actual=_f(t.value),
                       diff_pct=round(float(rel) * 100, 3), tolerance=_amt(eps, cur),
                       hard=not warn_only and (scale_slip or (status == "fail" and verified
                                                              and rel > Decimal("0.05") and soft is None)),
                       hint=hint))  # fmt: skip
        return True

    def _diagnose(self, ops: list[Fact], t: Fact, total: Decimal, lhs, signs) -> str | None:
        """Explain a failed identity: a 10^n scale slip on one operand, or a standalone figure in a
        consolidated identity (substituting the other basis's figure closes the gap)."""
        for i, f in enumerate([*ops, t]):
            for n in SCALE_POWERS:
                for mult in (Decimal(10) ** n, Decimal(10) ** -n):
                    fixed = f.value * mult
                    if i < len(ops):
                        new_total = total - Decimal(signs[i]) * f.value + Decimal(signs[i]) * fixed
                        ok = _rel(new_total, t.value) <= Decimal("0.002")
                    else:
                        ok = _rel(total, fixed) <= Decimal("0.002")
                    if ok:
                        return (f"C{f.id} ({f.metric}) looks {'10^' + str(n)}× off — a lakh/crore/million unit slip; "
                                f"the identity closes at {fixed.normalize():f} base units")  # fmt: skip
        for i, f in enumerate([*ops, t]):
            other = "standalone" if f.basis != "standalone" else "consolidated"
            for g in self.L.by.get((f.key, f.period or ""), []):
                if g.basis != other or g.value == f.value:
                    continue
                if i < len(ops):
                    new_total = total - Decimal(signs[i]) * f.value + Decimal(signs[i]) * g.value
                    ok = _rel(new_total, t.value) <= Decimal("0.002")
                else:
                    ok = _rel(total, g.value) <= Decimal("0.002")
                if ok:
                    return f"standalone vs consolidated mix-up: using C{g.id} ({other}) instead of C{f.id} closes it"
        return None

    # ---- P&L
    def pnl(self) -> None:
        keys = ("revenue", "other_income", "total_income", "total_expenses", "pbt_pre_exceptional", "exceptional",
                "pbt", "tax", "pat", "pat_owners", "pat_nci")  # fmt: skip
        periods = set().union(*(self.L.periods(k) for k in keys))
        had = {"income": False, "pbt": False, "pat": False, "split": False}
        for p in sorted(periods):
            for b in self.L.bases(keys, p):
                for cur in self.L.currencies(keys, p):
                    had["income"] |= self.sum_identity(
                        "pnl_total_income",
                        "Revenue + other income = total income",
                        p,
                        b,
                        cur,
                        [("revenue", 1), ("other_income", 1)],
                        "total_income",
                    )
                    if self.L.get("pbt_pre_exceptional", p, b, currency=cur):
                        had["pbt"] |= self.sum_identity(
                            "pnl_pbt",
                            "Total income − total expenses = profit before exceptional items and tax",
                            p,
                            b,
                            cur,
                            [("total_income", 1), ("total_expenses", -1)],
                            "pbt_pre_exceptional",
                        )
                        had["pbt"] |= self.sum_identity(
                            "pnl_exceptional",
                            "Profit before exceptional items ± exceptional items = PBT",
                            p,
                            b,
                            cur,
                            [("pbt_pre_exceptional", 1), ("exceptional", -1)],
                            "pbt",
                            signs_free=("exceptional",),
                        )
                    elif self.L.get("exceptional", p, b, currency=cur):
                        had["pbt"] |= self.sum_identity(
                            "pnl_pbt",
                            "Total income − total expenses ± exceptional items = PBT",
                            p,
                            b,
                            cur,
                            [("total_income", 1), ("total_expenses", -1), ("exceptional", -1)],
                            "pbt",
                            signs_free=("exceptional",),
                        )
                    else:
                        had["pbt"] |= self.sum_identity(
                            "pnl_pbt",
                            "Total income − total expenses = PBT",
                            p,
                            b,
                            cur,
                            [("total_income", 1), ("total_expenses", -1)],
                            "pbt",
                            soft="no exceptional items / share of associates in the ledger; they may explain the gap",
                        )
                    had["pat"] |= self.sum_identity(
                        "pnl_pat", "PBT − tax = PAT", p, b, cur, [("pbt", 1), ("tax", -1)], "pat"
                    )
                    had["split"] |= self.sum_identity(
                        "pnl_pat_split",
                        "Owners' share + NCI = PAT",
                        p,
                        b,
                        cur,
                        [("pat_owners", 1), ("pat_nci", 1)],
                        "pat",
                    )
        for k, name in (("income", "revenue + other income = total income"),
                        ("pbt", "total income − expenses = PBT"), ("pat", "PBT − tax = PAT")):  # fmt: skip
            if not had[k]:
                self.missing(name)

    # ---- EPS
    def _pat_for_eps(self, p: str, b: str, cur: str | None) -> list[Fact]:
        out = self.L.get("pat_owners", p, b, kind="money", currency=cur) + self.L.get(
            "pat", p, b, kind="money", currency=cur
        )
        return out

    def eps(self) -> None:
        any_eps = False
        implied: dict[tuple[str, str | None], list[tuple[str, Decimal, Fact, Fact]]] = {}
        for p in sorted(self.L.periods("eps_basic")):
            for b in self.L.bases(("eps_basic",), p):
                for e in self.L.get("eps_basic", p, b, kind="per_share"):
                    pats = self._pat_for_eps(p, b, e.currency)
                    if not pats or e.value == 0:
                        continue
                    # implied share count (for the restatement check): nearest to the others later
                    implied.setdefault((b, e.currency), []).extend(
                        (p, pt.value / e.value, e, pt) for pt in pats
                    )
                    shares = self.L.get("weighted_shares", p, b, kind="shares")
                    if not shares:
                        continue
                    any_eps = True
                    best = min(
                        ((pt, s, e.value * s.value) for pt in pats for s in shares),
                        key=lambda x: _rel(x[2], x[0].value),
                    )
                    pt, s, prod = best
                    rel = _rel(prod, pt.value)
                    tol = max(
                        EPS_TOL, e.ulp * 2 / abs(e.value)
                    )  # a 2-dp EPS of ₹3.26 carries 0.15 % rounding
                    status = "pass" if rel <= tol else ("warn" if rel <= 3 * tol else "fail")
                    self.add(Check(id=f"eps_identity:{p}:{b}:{e.currency}", family="eps_identity",
                                   title="EPS × weighted-average shares = PAT to owners", status=status, period=p,
                                   basis=b, formula="eps_basic × weighted_shares = pat_owners",
                                   detail=f"{e.raw.normalize():f} × {s.value:,.0f} = {_amt(prod, e.currency)} vs "
                                          f"{_amt(pt.value, e.currency)} ({float(rel) * 100:.2f}% apart; tolerance "
                                          f"{float(tol) * 100:.2f}%)",
                                   claims=[e.brief(), s.brief(), pt.brief()], expected=_f(prod), actual=_f(pt.value),
                                   diff_pct=round(float(rel) * 100, 3), tolerance=f"{float(tol) * 100:.2f}%",
                                   hard=status == "fail" and rel > Decimal("0.05") and
                                   all(x.status == "verified" for x in (e, s, pt))))  # fmt: skip
        if not any_eps:
            self.missing("EPS × weighted shares = PAT (no weighted-average share count in the ledger)")
        self._eps_basis(implied)

    def _eps_basis(
        self, implied: dict[tuple[str, str | None], list[tuple[str, Decimal, Fact, Fact]]]
    ) -> None:
        """Share counts implied by PAT ÷ EPS must agree across periods once a bonus / split is restated."""
        events = [f for f in self.facts if f.key in ("bonus", "split")]
        ev_note = (f"; corporate action in the ledger: C{events[0].id} {events[0].metric} ({events[0].period_raw})"
                   if events else "")  # fmt: skip
        done = False
        for (b, cur), rows in implied.items():
            # one implied count per period: the one closest to the median of all
            per: dict[str, tuple[Decimal, Fact, Fact]] = {}
            vals = sorted(r[1] for r in rows)
            med = vals[len(vals) // 2]
            for p, n, e, pt in rows:
                if p not in per or abs(n - med) < abs(per[p][0] - med):
                    per[p] = (n, e, pt)
            if len(per) < 2:
                continue
            done = True
            ref_p = max(per, key=lambda p: (int(p[-4:]), p.startswith("Q")))  # the latest period
            ref = per[ref_p][0]
            for p, (n, e, pt) in sorted(per.items()):
                if p == ref_p:
                    continue
                k = ref / n if n else Decimal(0)
                tol = Decimal("0.03") + e.ulp * 2 / abs(e.value)
                if _rel(n, ref) <= max(tol, Decimal("0.15")):
                    status, hard, hint = "pass", False, None
                    detail = (f"PAT ÷ EPS gives {n:,.0f} shares for {p} vs {ref:,.0f} for {ref_p}: the same share "
                              f"basis{' — restated for the corporate action' if events else ''}{ev_note}")  # fmt: skip
                elif k >= Decimal("1.8") and abs(k - k.to_integral_value()) / k <= Decimal("0.03"):
                    status, hard = "fail", True
                    detail = (f"PAT ÷ EPS gives {n:,.0f} shares for {p} but {ref:,.0f} for {ref_p} "
                              f"(≈ {k.to_integral_value()}×): {p} EPS is not restated for a bonus issue / split"
                              f"{ev_note}")  # fmt: skip
                    hint = (f"restate {p} EPS: divide by {k.to_integral_value()} (Ind AS 33 ¶64 — bonus and split "
                            f"adjustments apply to every period presented)")  # fmt: skip
                else:
                    status, hard, hint = (
                        "warn",
                        False,
                        "share count moved: fresh issue, buyback or a period mix-up",
                    )
                    detail = (f"PAT ÷ EPS gives {n:,.0f} shares for {p} vs {ref:,.0f} for {ref_p} "
                              f"({float(_rel(n, ref)) * 100:.1f}% apart){ev_note}")  # fmt: skip
                ref_fact = per[ref_p]
                self.add(Check(id=f"eps_basis:{p}:{b}:{cur}", family="eps_basis",
                               title="EPS share basis consistent across periods (bonus / split restated)",
                               status=status, period=p, basis=b, formula="pat ÷ eps_basic across periods",
                               detail=detail, claims=[e.brief(), pt.brief(), ref_fact[1].brief(), ref_fact[2].brief()],
                               expected=_f(ref), actual=_f(n), diff_pct=round(float(_rel(n, ref)) * 100, 3),
                               tolerance="15% (fresh issues and buybacks move the count)", hard=hard, hint=hint))  # fmt: skip
        if not done:
            self.missing("EPS share basis across periods (needs PAT and EPS for two periods)")

    # ---- balance sheet and cash
    def balance_sheet(self) -> None:
        keys = (
            "total_assets",
            "total_equity",
            "total_liabilities",
            "current_liabilities",
            "non_current_liabilities",
        )
        ok = False
        for p in sorted(self.L.periods("total_assets")):
            for b in self.L.bases(keys, p):
                for cur in self.L.currencies(keys, p):
                    if self.L.get("nci_equity", p, b, currency=cur):
                        ok |= self.sum_identity(
                            "bs_balance",
                            "Total assets = equity + NCI + liabilities",
                            p,
                            b,
                            cur,
                            [("total_equity", 1), ("nci_equity", 1), ("total_liabilities", 1)],
                            "total_assets",
                        )
                    ok |= self.sum_identity(
                        "bs_balance",
                        "Total assets = equity + total liabilities",
                        p,
                        b,
                        cur,
                        [("total_equity", 1), ("total_liabilities", 1)],
                        "total_assets",
                    )
                    ok |= self.sum_identity(
                        "bs_balance",
                        "Total assets = equity + current + non-current liabilities",
                        p,
                        b,
                        cur,
                        [("total_equity", 1), ("current_liabilities", 1), ("non_current_liabilities", 1)],
                        "total_assets",
                    )
        if not ok:
            self.missing("balance sheet balances (needs total assets, equity and total liabilities)")

    def cash(self) -> None:
        ok = False
        for p in sorted(self.L.periods("cfo")):
            for b in self.L.bases(("cfo", "cfi", "cff", "cash_close"), p):
                for cur in self.L.currencies(("cfo", "cash_close"), p):
                    opening = self.L.get("cash_open", p, b, currency=cur)
                    prev = _prev(p) if p.startswith("FY") else None
                    prev_close = self.L.get("cash_close", prev, b, currency=cur) if prev else []
                    open_key = "cash_open" if opening else ("cash_close@prev" if prev_close else None)
                    if open_key is None:
                        continue
                    if open_key == "cash_close@prev":
                        self.L.by.setdefault(("cash_open_from_prev", p), []).extend(prev_close)
                        open_key = "cash_open_from_prev"
                    lhs = [(open_key, 1), ("cfo", 1), ("cfi", 1), ("cff", 1)]
                    if self.L.get("fx_cash", p, b, currency=cur):
                        lhs.append(("fx_cash", 1))
                    ok |= self.sum_identity(
                        "cash_rollforward",
                        "Opening cash + CFO + CFI + CFF (+ FX) = closing cash",
                        p,
                        b,
                        cur,
                        lhs,
                        "cash_close",
                        soft="bank overdrafts netted in cash (Ind AS 7) or FX effects may explain",
                    )
        if not ok:
            self.missing("cash roll-forward (needs opening and closing cash with CFO, CFI and CFF)")

    # ---- recomputed ratios
    def _rate_check(
        self, family: str, title: str, stated: Fact, options: list[tuple[str, Decimal, list[Fact]]]
    ) -> None:
        """A stated rate against its recomputation under each accepted definition; pass if any matches."""
        tol_abs = stated.ulp + Decimal("0.0005")  # half the printed unit + 0.05 pp
        best = min(options, key=lambda o: abs(o[1] - stated.value))
        name, val, parts = best
        diff = abs(val - stated.value)
        ok = diff <= tol_abs or _rel(val, stated.value) <= Decimal("0.005")
        status = "pass" if ok else ("warn" if diff <= Decimal("0.0025") else "fail")
        all_ver = all(f.status == "verified" for f in (stated, *parts))
        self.add(Check(id=f"{family}:{stated.period or stated.period_raw}:{stated.basis}:{stated.id}", family=family,
                       title=title, status=status, period=stated.period or stated.period_raw, basis=stated.basis,
                       formula=name, detail=f"recomputed {_pct(val)} ({name}) vs stated {_pct(stated.value)}"
                       f" — {float(diff) * 100:.2f} pp apart",
                       claims=[stated.brief(), *(f.brief() for f in parts)], expected=_f(val), actual=_f(stated.value),
                       diff_pct=round(float(_rel(val, stated.value)) * 100, 3),
                       tolerance=f"{float(tol_abs) * 100:.3f} pp", hard=status == "fail" and all_ver and
                       diff >= Decimal("0.01"),
                       hint=None if ok else "the stated rate does not follow from the ledger's own figures"))  # fmt: skip

    def margins(self) -> None:
        n = 0
        for mkey, num_keys, title in (("ebitda_margin", ("ebitda",), "EBITDA margin recomputed"),
                                      ("pat_margin", ("pat", "pat_owners"), "PAT margin recomputed")):  # fmt: skip
            for m in [f for f in self.facts if f.key == mkey and f.kind == "pct" and f.period]:
                opts = []
                for nk in num_keys:
                    for num in self.L.get(nk, m.period, m.basis, kind="money"):
                        for dk in ("revenue", "total_income"):
                            for den in self.L.get(dk, m.period, m.basis, kind="money", currency=num.currency):
                                if den.value:
                                    opts.append((f"{nk} ÷ {dk}", num.value / den.value, [num, den]))
                if opts:
                    n += 1
                    self._rate_check("margin", title, m, opts)
        if not n:
            self.missing("margins recomputed (needs a stated margin with its numerator and revenue)")

    def growth(self) -> None:
        n = 0
        for g in [f for f in self.facts if f.key.endswith(("_growth", "_cagr")) and f.kind == "pct"]:
            base = g.key.rsplit("_", 1)[0]
            span = parse_growth_period(g.period_raw, g.metric)
            if not span:
                continue
            a, b, years = span
            if years <= 0:
                continue
            cur = "USD" if "usd" in g.metric.lower() else "INR"
            bases_ = [base, "pat_owners"] if base == "pat" else [base]
            kind = "per_share" if base == "eps_basic" else "money"
            opts = []
            for bk in bases_:
                for x0 in self.L.get(bk, a, g.basis, kind=kind, currency=cur):
                    for x1 in self.L.get(bk, b, g.basis, kind=kind, currency=cur):
                        if x0.value > 0 and x1.value > 0:
                            r = (x1.value / x0.value) ** (Decimal(1) / Decimal(years)) - 1
                            name = f"({bk} {b} ÷ {bk} {a})" + (f"^(1/{years})" if years > 1 else "") + " − 1"
                            opts.append((name, r, [x0, x1]))
            if opts:
                n += 1
                self._rate_check(
                    "growth", f"{'CAGR' if years > 1 else 'Growth'} recomputed ({g.metric})", g, opts
                )
        if not n:
            self.missing("growth rates recomputed (needs a stated growth rate and both periods' figures)")

    # ---- unit / scale (C.2), basis mix, cross-document (C.3), sanity bands
    def scale_and_basis(self) -> None:
        groups: dict[tuple[str, str], list[Fact]] = {}
        for f in self.facts:
            if f.kind in ("money", "per_share", "shares") and f.period:
                groups.setdefault((f.key or f.metric.lower(), f.period), []).append(f)
        for (_, p), items in groups.items():
            for a, b in itertools.combinations(items, 2):
                if a.basis != b.basis and "unknown" not in (a.basis, b.basis):
                    continue
                if a.value == 0 or b.value == 0:
                    continue
                if a.kind == b.kind and a.currency == b.currency:
                    r = abs(a.value / b.value)
                    for n in SCALE_POWERS:
                        for mult in (Decimal(10) ** n, Decimal(10) ** -n):
                            if _rel(r, mult) <= SCALE_TOL:
                                self.add(Check(
                                    id=f"scale_shift:{p}:{a.id}:{b.id}", family="scale_shift",
                                    title="Same figure recorded at two scales (lakh / crore / million mix-up)",
                                    status="fail", period=p, basis=a.basis,
                                    formula=f"C{a.id} ÷ C{b.id} = 10^{n if mult > 1 else -n}",
                                    detail=f"{a.metric} {a.raw.normalize():f} {a.unit or ''} vs {b.metric} "
                                           f"{b.raw.normalize():f} {b.unit or ''} — {mult.normalize():f}× apart",
                                    claims=[a.brief(), b.brief()], expected=_f(b.value), actual=_f(a.value),
                                    hard=True, hint="one of the two units is wrong; check the table's "
                                                    "'₹ in million / crore / lakh' header"))  # fmt: skip
                elif (
                    a.kind == b.kind == "money"
                    and {a.currency, b.currency} == {"INR", "USD"}
                    and a.raw == b.raw
                ):
                    self.add(Check(id=f"currency_label:{p}:{a.id}:{b.id}", family="scale_shift",
                                   title="Same number labelled ₹ in one claim and US$ in another", status="fail",
                                   period=p, basis=a.basis, formula="raw values equal, currencies differ",
                                   detail=f"C{a.id} {a.raw.normalize():f} {a.unit} vs C{b.id} {b.raw.normalize():f} "
                                          f"{b.unit}", claims=[a.brief(), b.brief()], hard=True,
                                   hint="a US$ figure read as ₹ (or the reverse)"))  # fmt: skip
            # basis mix: an unlabelled figure equal to the standalone one while consolidated differs
            stand = [f for f in items if f.basis == "standalone"]
            cons = [f for f in items if f.basis == "consolidated"]
            for u in [f for f in items if f.basis == "unknown"]:
                s = next(
                    (x for x in stand if x.currency == u.currency and _rel(x.value, u.value) <= REL_EPS), None
                )
                c = next((x for x in cons if x.currency == u.currency and _rel(x.value, u.value) > Decimal("0.005")),
                         None)  # fmt: skip
                if s and c:
                    self.add(Check(id=f"basis_mix:{p}:{u.id}", family="basis_mix",
                                   title="Unlabelled figure is the standalone one (consolidated differs)",
                                   status="warn", period=p, basis="unknown",
                                   formula=f"C{u.id} = standalone C{s.id} ≠ consolidated C{c.id}",
                                   detail=f"{u.metric} {u.raw.normalize():f} matches standalone C{s.id}; consolidated "
                                          f"C{c.id} is {c.raw.normalize():f}",
                                   claims=[u.brief(), s.brief(), c.brief()],
                                   hint="label the basis; a consolidated report should use the consolidated figure"))  # fmt: skip
            # cross-document differences (restated vs audited vs later filings)
            for a, b in itertools.combinations(items, 2):
                if (a.docs and b.docs and not (a.docs & b.docs) and a.basis == b.basis and a.currency == b.currency
                        and a.kind == b.kind and _rel(a.value, b.value) > Decimal("0.005")):  # fmt: skip
                    if any(
                        c.family == "scale_shift" and {a.id, b.id} <= set(c.claim_ids)
                        for c in self.report.checks
                    ):
                        continue
                    self.add(Check(id=f"cross_document:{p}:{a.id}:{b.id}", family="cross_document",
                                   title="Same metric and period differs between documents (restated vs audited / "
                                         "later filing)", status="warn", period=p, basis=a.basis,
                                   formula=f"C{a.id} vs C{b.id}",
                                   detail=f"{a.metric} {a.raw.normalize():f} ({', '.join(sorted(a.docs))[:80]}) vs "
                                          f"{b.raw.normalize():f} ({', '.join(sorted(b.docs))[:80]}) — "
                                          f"{float(_rel(a.value, b.value)) * 100:.2f}% apart",
                                   claims=[a.brief(), b.brief()],
                                   hint="must be explained by the restatement-adjustments note; otherwise one "
                                        "figure is misread"))  # fmt: skip

    def sanity(self) -> None:
        bands = {"pat_margin": (Decimal(-2), Decimal("0.8"), "PAT margin within −200%…80%"),
                 "pe": (Decimal(0), Decimal(500), "P/E within 0…500"),
                 "receivable_days": (Decimal(0), Decimal(730), "receivable days within 0…730")}  # fmt: skip
        for f in self.facts:
            if f.key in bands and f.kind in ("pct", "multiple", "days"):
                lo, hi, title = bands[f.key]
                if not lo <= f.value <= hi:
                    self.add(Check(id=f"sanity:{f.id}", family="sanity", title=f"Sanity band: {title}", status="warn",
                                   period=f.period or f.period_raw, basis=f.basis, formula=title,
                                   detail=f"{f.metric} = {f.raw.normalize():f} {f.unit or ''} is outside the band",
                                   claims=[f.brief()], hint="possible unit slip (% vs fraction, x vs %)"))  # fmt: skip

    # ---- statement checks (#518). The idea is the model-integrity list of anthropics/financial-services' `audit-xls`
    # skill, rewritten for Ind AS ledgers. All six are warn-level: a result is pass or warn, never fail, never `hard`,
    # so none can block a report; a check without its inputs is listed as "not checked: <input>", never passed.
    def _money(self, key: str, period: str | None, basis: str, cur: str | None) -> list[Fact]:
        return self.L.get(key, period, basis, kind="money", currency=cur)

    def equity_rollforward(self) -> None:
        """Opening + profit for the year − dividends ± stated movements = closing, for other equity and for retained
        earnings (annual; the opening balance is the previous year's closing one). Dividends are required: a ledger
        without them is "not checked", never read as "no dividend". Buybacks, share-based payments and OCI join when
        the ledger states them; the ones it does not state are the usual reason for a gap."""
        name = "equity roll-forward"
        stocks = (("other_equity", "Other equity"), ("retained_earnings", "Retained earnings"))
        if not any(self.L.periods(k) for k, _ in stocks):
            self.not_checked(name, "no other equity or retained earnings claim in the ledger")
            return
        soft = ("movements the ledger does not hold may explain the gap (other comprehensive income, shares issued "
                "at a premium, buybacks, share-based payments, transfers between reserves): record them as claims")  # fmt: skip
        for stock, label in stocks:
            for p in sorted(q for q in self.L.periods(stock) if q.startswith("FY")):
                prev = _prev(p)
                for b in self._bases((stock,), (p, prev)):
                    for cur in self.L.currencies((stock,), p):
                        if not self._money(stock, p, b, cur):
                            continue
                        opening, paid = self._money(stock, prev, b, cur), self._money("dividends", p, b, cur)
                        profit = self._money("pat_owners", p, b, cur) or self._money("pat", p, b, cur)
                        absent = [n for n, held in (("the opening balance (the previous year's closing figure)", opening),
                                                    ("profit for the year", profit), ("dividends paid", paid)) if not held]  # fmt: skip
                        if absent:
                            self.not_checked(name, ", ".join(absent), self._where(p, b))
                            continue
                        lhs = [(self._as("opening_balance", p, opening), 1), (self._as("profit", p, profit), 1),
                               (self._as("dividends_out", p, paid, magnitude=True), -1)]  # fmt: skip
                        for key, role, sign in (("buyback", "buyback_out", -1), ("esop_charge", "share_based_payments", 1)):  # fmt: skip
                            if held := self._money(key, p, b, cur):
                                lhs.append((self._as(role, p, held, magnitude=True), sign))
                        if self._money("oci", p, b, cur):
                            lhs.append(("oci", 1))  # signed as recorded: a loss in OCI is negative
                        self.sum_identity(
                            "equity_rollforward",
                            f"{label} roll-forward: opening + profit − dividends ± stated movements = closing",
                            p, b, cur, lhs, stock, soft=soft, warn_only=True, tag=f":{stock}")  # fmt: skip

    def da_tie(self) -> None:
        """D&A added back in the cash-flow statement = D&A charged in the P&L (same period, same basis)."""
        name = "D&A tie (cash flow vs P&L)"
        periods = self.L.periods("da") | self.L.periods("da_cf")
        if not periods:
            self.not_checked(name, "no depreciation and amortisation claim in the ledger")
        for p in sorted(periods):
            for b in self.L.bases(("da", "da_cf"), p):
                for cur in self.L.currencies(("da", "da_cf"), p):
                    pnl, cf = self._money("da", p, b, cur), self._money("da_cf", p, b, cur)
                    if pnl and cf:
                        self.sum_identity(
                            "da_tie", "D&A added back in the cash flow = D&A charged in the P&L", p, b, cur,
                            [(self._as("pnl_charge", p, pnl, magnitude=True), 1)],
                            self._as("cash_flow_add_back", p, cf, magnitude=True), warn_only=True,
                            soft="the add-back normally equals the charge: a gap means discontinued operations, an "
                                 "impairment inside one of the lines, or a misread line")  # fmt: skip
                    elif pnl or cf:
                        self.not_checked(name, "the add-back in the cash-flow statement (only the P&L charge is held)"
                                         if pnl else "the P&L charge (only the cash-flow add-back is held)",
                                         self._where(p, b))  # fmt: skip

    def tax_band(self) -> None:
        """Effective tax rate (tax ÷ PBT) inside `TAX_BAND`. Without a tax-expense claim the rate implied by PBT − PAT
        is used and labelled so; a stated effective_tax_rate is banded when neither can be computed. A rate outside
        the band passes only when the ledger holds the reason for the same period and basis (deferred tax, MAT
        credit, exceptional items, a tax reversal); a rate on a loss is not checked."""
        name = "effective tax rate"
        done: set[tuple[str, str]] = set()
        for p in sorted(self.L.periods("pbt")):
            for b in self.L.bases(("pbt", "tax", "pat"), p):
                for cur in self.L.currencies(("pbt",), p):
                    pbts, tax, pat = (self._money(k, p, b, cur) for k in ("pbt", "tax", "pat"))
                    if not pbts:
                        continue
                    pbt = pbts[0]
                    if pbt.value <= 0:
                        self.not_checked(name, "PBT is zero or negative (a rate on a loss says nothing)",
                                         self._where(p, b))  # fmt: skip
                    elif tax:
                        self._tax_check(tax[0].value / pbt.value, "tax ÷ pbt", [tax[0], pbt], p, b, cur)
                    elif pat:
                        self._tax_check((pbt.value - pat[0].value) / pbt.value,
                                        "(pbt − pat) ÷ pbt — implied, no tax-expense claim in the ledger",
                                        [pbt, pat[0]], p, b, cur)  # fmt: skip
                    else:
                        self.not_checked(name, "tax expense (and no PAT to imply it from)", self._where(p, b))
                        continue
                    done.add((p, b))
        stated = [f for f in self.facts if f.key == "etr" and f.kind == "pct" and f.period]
        for e in stated:
            if not any(p == e.period and (b == e.basis or "unknown" in (b, e.basis)) for p, b in done):
                self._tax_check(e.value, "effective_tax_rate as stated", [e], e.period or "", e.basis, None)
        if not self.L.periods("pbt") and not stated:
            self.not_checked(name, "no PBT claim in the ledger")

    def _tax_check(
        self, rate: Decimal, formula: str, used: list[Fact], p: str, b: str, cur: str | None
    ) -> None:
        lo, hi = TAX_BAND
        # the company's regime is not known here, so the result shows every reference rate, not a "nearest" one
        refs = "; ".join(f"{_pct(r)} {label.split(':')[0]}" for label, r in STATUTORY_TAX_RATES)
        detail = f"effective tax rate {_pct(rate)} ({formula}); band {_pct(lo)}…{_pct(hi)}"
        why: list[Fact] = []
        status, hint = "pass", None
        if not lo <= rate <= hi:
            why = [f for k in ("deferred_tax", "mat_credit", "exceptional", "tax_adjustment")
                   for f in self._money(k, p, b, cur) if f.value]  # fmt: skip
            if why:
                detail += "; outside the band, with the reason in the ledger: " + ", ".join(
                    f"C{f.id} {f.metric}" for f in why
                )
                hint = "say the reason for the unusual tax rate in the text"
            else:
                status = "warn"
                hint = ("outside the band with no reason in the ledger: record it as a claim (deferred tax, MAT credit "
                        "under s.115JAA, exceptional items, tax of earlier years) or recheck the tax and PBT figures")  # fmt: skip
        self.add(Check(id=f"tax_band:{p}:{b}:{cur or 'stated'}", family="tax_band",
                       title="Effective tax rate inside the statutory band", status=status, period=p, basis=b,
                       formula=formula, detail=detail, claims=[f.brief() for f in (*used, *why)], actual=_f(rate),
                       tolerance=f"{_pct(lo)}…{_pct(hi)}: the statutory rates ({refs}) with "
                                 f"{float(TAX_BAND_CUSHION) * 100:.0f} pp either side", hint=hint))  # fmt: skip

    def margin_order(self) -> None:
        """Gross margin ≥ EBITDA margin ≥ PAT margin for a non-lender, over the margins the ledger states or lets
        compute on revenue (two of the three are enough; the absent one is named). Skipped for a bank, NBFC or
        insurer, with the reason."""
        name = "margin ordering (gross ≥ EBITDA ≥ PAT)"
        if lender := self._lender():
            self.missing(f"{name} — skipped: {lender}; a bank, NBFC or insurer has no gross margin or EBITDA")
            return
        tiers = (("gross", "gross_margin", ("gross_profit",)), ("EBITDA", "ebitda_margin", ("ebitda",)),
                 ("PAT", "pat_margin", ("pat", "pat_owners")))  # fmt: skip
        keys = [k for _, mkey, nums in tiers for k in (mkey, *nums)]
        seen_any = False
        for p in sorted(set().union(*(self.L.periods(k) for k in keys))):
            for b in self.L.bases(keys, p):
                held: list[tuple[str, Decimal, list[Fact], Decimal]] = []  # tier, margin, claims, rounding
                for tier, mkey, nums in tiers:
                    if stated := self.L.get(mkey, p, b, kind="pct"):
                        held.append((tier, stated[0].value, [stated[0]], stated[0].ulp))
                    elif pair := next(((num, den) for nk in nums for num in self.L.get(nk, p, b, kind="money")
                                       for den in self._money("revenue", p, b, num.currency) if den.value), None):  # fmt: skip
                        held.append((tier, pair[0].value / pair[1].value, list(pair), Decimal(0)))
                if not held:
                    continue
                seen_any = True
                if len(held) < 2:
                    self.not_checked(name, "a second margin (only one of gross, EBITDA and PAT margin is held or "
                                           "computable on revenue)", self._where(p, b))  # fmt: skip
                    continue
                breaks = [f"{lo[0]} margin {_pct(lo[1])} is above {hi[0]} margin {_pct(hi[1])}"
                          for hi, lo in itertools.pairwise(held) if lo[1] > hi[1] + hi[3] + lo[3] + Decimal("0.0005")]  # fmt: skip
                absent = [t for t, *_ in tiers if t not in {h[0] for h in held}]
                detail = "; ".join(breaks) or " ≥ ".join(f"{t} {_pct(v)}" for t, v, *_ in held)
                if absent:
                    detail += f" ({absent[0]} margin not in the ledger: not checked)"
                used = {f.id: f for h in held for f in h[2]}
                self.add(Check(id=f"margin_order:{p}:{b}", family="margin_order",
                               title="Margins in order: gross ≥ EBITDA ≥ PAT", status="warn" if breaks else "pass",
                               period=p, basis=b, formula=" ≥ ".join(f"{t} margin" for t, *_ in held), detail=detail,
                               claims=[f.brief() for f in used.values()],
                               hint="other income or an exceptional gain can lift PAT above EBITDA: say so in the "
                                    "text; otherwise one margin is on another basis or misread" if breaks else None))  # fmt: skip
        if not seen_any:
            self.not_checked(name, "no margin, and no EBITDA or PAT with revenue, in the ledger")

    def capex_fixed_assets(self) -> None:
        """Cash capex against the year's rise in gross block, plus the rise in CWIP when both year ends hold it
        (annual). Within `CAPEX_BAND` of the larger figure passes; a wider gap needs a reason in the text."""
        name = "capex vs gross block"
        years = sorted({q for k in ("capex", "gross_block") for q in self.L.periods(k) if q.startswith("FY")})
        if not years:
            self.not_checked(name, "no capex or gross block claim in the ledger")
        for p in years:
            prev = _prev(p)
            for b in self._bases(("capex", "gross_block"), (p, prev)):
                for cur in self.L.currencies(("capex", "gross_block"), p):
                    capex, gb1, gb0 = (self._money(k, q, b, cur)
                                       for k, q in (("capex", p), ("gross_block", p), ("gross_block", prev)))  # fmt: skip
                    if not capex and not (gb1 and gb0):
                        continue  # the opening year's block alone asks no question
                    absent = [n for n, held in (("capex", capex), ("gross block at the year end", gb1),
                                                ("gross block at the previous year end", gb0)) if not held]  # fmt: skip
                    if absent:
                        self.not_checked(name, ", ".join(absent), self._where(p, b))
                        continue
                    used, move = [capex[0], gb1[0], gb0[0]], gb1[0].value - gb0[0].value
                    note = "CWIP is not held for both year ends, so its movement is left out"
                    w1, w0 = self._money("cwip", p, b, cur), self._money("cwip", prev, b, cur)
                    if w1 and w0:
                        used, move, note = (
                            [*used, w1[0], w0[0]],
                            move + w1[0].value - w0[0].value,
                            "with CWIP",
                        )
                    spend = abs(
                        capex[0].value
                    )  # the cash-flow line is often recorded as an outflow (negative)
                    big = max(spend, abs(move))
                    rel = abs(spend - move) / big if big else Decimal(0)
                    ok = rel <= CAPEX_BAND
                    self.add(Check(id=f"capex_fixed_assets:{p}:{b}:{cur}", family="capex_fixed_assets",
                                   title="Capex against the rise in gross block (and CWIP)",
                                   status="pass" if ok else "warn", period=p, basis=b,
                                   formula="capex ≈ Δ gross_block + Δ cwip",
                                   detail=f"capex {_amt(spend, cur)} vs a rise of {_amt(move, cur)} in gross block "
                                          f"({note}) — {float(rel) * 100:.1f}% apart",
                                   claims=[f.brief() for f in used], expected=_f(move), actual=_f(spend),
                                   diff_pct=round(float(rel) * 100, 3),
                                   tolerance=f"{float(CAPEX_BAND) * 100:.0f}% of the larger figure",
                                   hint=None if ok else "disposals, capital advances and creditors, leased assets, an "
                                        "acquisition or FX translation can explain it: say which in the text"))  # fmt: skip

    def growth_outliers(self) -> None:
        """A headline line (`HEADLINE`) that more than doubles or changes sign against the same period a year
        earlier, from the two figures themselves (same basis, same unit kind, same currency); a stated year-on-year
        growth above 100 % counts when the two figures are not both held. Always a warning: the checker cannot read
        the report, so it asks for the explanation."""
        n = 0
        paired: set[tuple[str, str]] = set()
        hint = ("needs an explanation in the text: what drove it (an acquisition, a low base, a one-off, a change of "
                "basis or unit)")  # fmt: skip
        for key, label in HEADLINE:
            for p in sorted(self.L.periods(key)):
                prev = _prev(p)
                for b in self._bases((key,), (p, prev)):
                    for kind in ("per_share", "money") if key == "eps_basic" else ("money",):
                        for cur in sorted(
                            {f.currency for f in self.L.get(key, p, b, kind=kind) if f.currency}
                        ):
                            now = self.L.get(key, p, b, kind=kind, currency=cur)
                            then = self.L.get(key, prev, b, kind=kind, currency=cur)
                            if not now or not then:
                                continue
                            x1, x0 = now[0], then[0]
                            n += 1
                            paired.add((key, p))
                            change = "nil in both periods"
                            if x0.value == 0:
                                flag = "from nil a year earlier" if x1.value else None
                            elif x0.value * x1.value < 0:
                                flag = (
                                    "a sign change ("
                                    + ("loss to profit" if x1.value > 0 else "profit to loss")
                                    + ")"
                                )
                            else:
                                change = f"{float((x1.value - x0.value) / abs(x0.value)) * 100:+.1f}%"
                                flag = change if abs(x1.value) > GROWTH_FLAG * abs(x0.value) else None
                            show = [
                                _amt(x.value, cur) if kind == "money" else f"{x.raw.normalize():f}"
                                for x in (x1, x0)
                            ]
                            self.add(Check(id=f"growth_outlier:{key}:{p}:{b}:{kind}:{cur}", family="growth_outlier",
                                           title=f"Year-on-year change in a headline line ({label})",
                                           status="warn" if flag else "pass", period=p, basis=b,
                                           formula=f"{key} {p} vs {key} {prev}",
                                           detail=f"{label} {p} {show[0]} vs {prev} {show[1]}: "
                                                  + (flag or f"{change}, not above 100 % and no sign change"),
                                           claims=[x1.brief(), x0.brief()], expected=_f(x0.value), actual=_f(x1.value),
                                           tolerance="above 100 % or a sign change", hint=hint if flag else None))  # fmt: skip
        for g in [f for f in self.facts if f.key.endswith("_growth") and f.kind == "pct"]:
            span = parse_growth_period(g.period_raw, g.metric)
            base = g.key.rsplit("_", 1)[0]
            if not span or span[2] != 1 or {(base, span[1]), (f"{base}_owners", span[1])} & paired:
                continue
            n += 1
            big = abs(g.value) > 1
            self.add(Check(id=f"growth_outlier:stated:{g.id}", family="growth_outlier",
                           title=f"Year-on-year change in a headline line ({g.metric})",
                           status="warn" if big else "pass", period=span[1], basis=g.basis,
                           formula=f"{g.metric} as stated", detail=f"stated growth {_pct(g.value)}"
                           + ("" if big else ", not above 100 %"), claims=[g.brief()], actual=_f(g.value),
                           tolerance="above 100 %", hint=hint if big else None))  # fmt: skip
        if not n:
            self.not_checked("growth outliers", "no headline line (revenue, EBITDA, PBT, PAT, basic EPS) is held for "
                                                "the same period a year apart on one basis")  # fmt: skip

    def run(self) -> IdentityReport:
        self.pnl()
        self.eps()
        self.balance_sheet()
        self.cash()
        self.margins()
        self.growth()
        self.scale_and_basis()
        self.sanity()
        self.equity_rollforward()
        self.da_tie()
        self.tax_band()
        self.margin_order()
        self.capex_fixed_assets()
        self.growth_outliers()
        self._flush_not_checked()
        return self.report


def check_claims(claims: Iterable[dict[str, Any]]) -> IdentityReport:
    """Run every identity / recomputation / scale check over plain claim dicts. Pure; never writes."""
    facts, _ = facts_from(claims)
    return _Engine(facts).run()


# --------------------------------------------------------------------------- database wrappers
def claim_dicts(session, run_id: int) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from finresearch.db.models import Claim

    rows = session.scalars(select(Claim).where(Claim.run_id == run_id).order_by(Claim.id)).all()
    return [{"id": c.id, "stream": c.stream, "statement": c.statement, "claim_type": c.claim_type,
             "metric": c.metric, "value": str(c.value) if c.value is not None else None, "unit": c.unit,
             "period": c.period, "importance": c.importance, "status": c.status,
             "corrects_claim_id": c.corrects_claim_id,
             "citations": [{"document_id": x.document_id, "url": x.url} for x in c.citations]} for c in rows]  # fmt: skip


def run_identities(session, run_id: int, *, record: bool = False) -> IdentityReport:
    """Identity checks for a stored run. With `record`, each failing / warning check id is added to its claims'
    `checks["identities"]` (statuses are never changed here; the publish gate applies the severity policy)."""
    rep = check_claims(claim_dicts(session, run_id))
    if record:
        from finresearch.db.models import Claim

        by_claim: dict[int, list[str]] = {}
        for c in rep.failing():
            for i in c.claim_ids:
                by_claim.setdefault(i, []).append(f"{c.status}: {c.id}")
        for cid, items in by_claim.items():
            cl = session.get(Claim, cid)
            if cl is not None:
                cl.checks = {**(cl.checks or {}), "identities": items}
        session.flush()
    return rep

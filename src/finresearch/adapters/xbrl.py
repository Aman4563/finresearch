"""Facts from the XBRL of an NSE/BSE financial-results filing (Ind AS 'in-bse-fin' taxonomy).

Only undimensioned facts are read (segment and expense-detail facts carry an xbrli:scenario). NSE's results
XBRL names the current period "OneD" and the year-to-date period "FourD", and the year-to-date context's own
xbrli:period is sometimes wrong; each context's DateOfStart/EndOfReportingPeriod facts are authoritative.
Monetary values are in rupees.

Integrated Filing (Financials) XBRL (from the Mar-2025 quarter) keeps the same element names under a newer
"in-capmkt" namespace, so matching is on local names. Its entity identifier is the BSE scrip code, not the NSE
symbol. Banks file the banking taxonomy: interest earned stands in for revenue, and their profit/EPS elements are
mapped onto the same keys (the taxonomies never carry both names). Insurers file life ("LI") or general ("GI")
insurance taxonomies: net premium income (life) or premium earned (general) stands in for revenue.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

XBRLI = "{http://www.xbrl.org/2003/instance}"
KEY_FACTS = {
    "RevenueFromOperations": "revenue_from_operations",
    "OtherIncome": "other_income",
    "Income": "total_income",
    "Expenses": "total_expenses",
    "FinanceCosts": "finance_costs",
    "DepreciationDepletionAndAmortisationExpense": "depreciation",
    "EmployeeBenefitExpense": "employee_benefit_expense",
    "ExceptionalItemsBeforeTax": "exceptional_items",
    "ProfitBeforeTax": "profit_before_tax",
    "TaxExpense": "tax_expense",
    "ProfitLossForPeriod": "profit_for_period",
    "ProfitOrLossAttributableToOwnersOfParent": "profit_attributable_to_owners",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_basic",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_diluted",
    "PaidUpValueOfEquityShareCapital": "paid_up_equity_capital",
    "FaceValueOfEquityShareCapital": "face_value",
    # banking taxonomy
    "InterestEarned": "interest_earned",
    "InterestExpended": "interest_expended",
    "ExpenditureExcludingProvisionsAndContingencies": "expenditure_excluding_provisions",
    "ProvisionsOtherThanTaxAndContingencies": "provisions",
    "ExceptionalItems": "exceptional_items",
    "ProfitLossFromOrdinaryActivitiesBeforeTax": "profit_before_tax",
    "ProfitLossForThePeriod": "profit_for_period",
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates": "profit_attributable_to_owners",
    "BasicEarningsPerShareAfterExtraordinaryItems": "eps_basic",
    "DilutedEarningsPerShareAfterExtraordinaryItems": "eps_diluted",
    # insurance taxonomies (life "LI", general "GI"): premium stands in for revenue; profit is the P&L
    # (shareholders') account's
    "NetPremiumIncome": "net_premium_income",
    "PremiumEarned": "premium_earned",
    "ProfitLossBeforeTax": "profit_before_tax",
    "ProfitOrLossBeforeTax": "profit_before_tax",
    "ProvisionsForTaxes": "tax_shareholders_account",  # life: the P&L account's tax
    "ProvisionForTax": "provision_for_tax",  # general: P&L tax; life: the policyholders' account's tax
    "ProfitLossAfterTaxAndExtraordinaryItems": "profit_for_period",
    "ProfitLossAfterTax": "profit_for_period",
    "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized": "eps_basic",
}


@dataclass
class PeriodFacts:
    context: str
    start: date | None
    end: date | None
    facts: dict[str, Decimal] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)


@dataclass
class ResultsXbrl:
    symbol: str | None
    consolidated: bool | None
    audited: bool | None
    periods: dict[str, PeriodFacts]

    @property
    def quarter(self) -> PeriodFacts | None:
        return self.periods.get("OneD")

    @property
    def year_to_date(self) -> PeriodFacts | None:
        return self.periods.get("FourD")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_results_xbrl(data: bytes) -> ResultsXbrl:
    root = ET.fromstring(data)
    dimensioned, ctx_period, symbol = set(), {}, None
    for ctx in root.iter(f"{XBRLI}context"):
        cid = ctx.get("id")
        if ctx.find(f"{XBRLI}scenario") is not None or ctx.find(f".//{XBRLI}segment") is not None:
            dimensioned.add(cid)
        ident = ctx.find(f".//{XBRLI}identifier")
        symbol = symbol or (ident.text.strip() if ident is not None and ident.text else None)
        s, e = ctx.find(f".//{XBRLI}startDate"), ctx.find(f".//{XBRLI}endDate")
        ctx_period[cid] = (date.fromisoformat(s.text) if s is not None else None,
                           date.fromisoformat(e.text) if e is not None else None)  # fmt: skip
    periods: dict[str, PeriodFacts] = {}
    text_facts: dict[tuple[str, str], str] = {}
    for el in root:
        cid = el.get("contextRef")
        if not cid or cid in dimensioned:
            continue
        name, value = _local(el.tag), (el.text or "").strip()
        text_facts[(cid, name)] = value
        if name in KEY_FACTS and value:
            try:
                num = Decimal(value)
            except InvalidOperation:
                continue
            p = periods.setdefault(cid, PeriodFacts(cid, *ctx_period.get(cid, (None, None))))
            p.facts[KEY_FACTS[name]] = num
            p.units[KEY_FACTS[name]] = el.get("unitRef") or ""
    for cid, p in periods.items():  # the filing's own period facts override the context period
        if s := text_facts.get((cid, "DateOfStartOfReportingPeriod")):
            p.start = date.fromisoformat(s)
        if e := text_facts.get((cid, "DateOfEndOfReportingPeriod")):
            p.end = date.fromisoformat(e)
    nature = next((v for (c, n), v in text_facts.items() if n == "NatureOfReportStandaloneConsolidated"), "")
    audited = next((v for (c, n), v in text_facts.items() if n == "WhetherResultsAreAuditedOrUnaudited"), "")
    return ResultsXbrl(symbol=symbol, consolidated=None if not nature else nature.lower().startswith("consolidated"),
                       audited=None if not audited else audited.lower() == "audited", periods=periods)  # fmt: skip

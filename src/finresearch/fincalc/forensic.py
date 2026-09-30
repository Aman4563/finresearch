"""Forensic and quality scores: Piotroski F-score, Altman Z''-EM, Beneish M-score (8 variables), accruals and cash
conversion (docs/dev/RESEARCH_ROADMAP.md §C.5, item 11).

These are SCREENING FLAGS, never buy/sell triggers: none of them has been validated on Indian data that the roadmap
could verify [W]. Sources:
- Piotroski 2000, "Value investing: the use of historical financial statement information", JAR 38 (roadmap [8]).
- Altman 2005, "An emerging market credit scoring system for corporate bonds", Emerging Markets Review 6 ([7]).
- Beneish 1999, "The detection of earnings manipulation", Financial Analysts Journal 55(5) ([6]).
- Sloan 1996, "Do stock prices fully reflect information in accruals and cash flows...", The Accounting Review ([9]).

Inputs are one fiscal year each (`cur` = latest, `prev` = the year before) as mappings of the keys that
`adapters.xbrl.parse_results_xbrl` produces from an annual Integrated Filing (the year-to-date P&L and cash flow plus
the year-end statement of assets and liabilities), all in the same unit and on the same basis (consolidated or
standalone). A needed line that was not reported makes the score None with the missing keys listed: nothing is
guessed. Where Ind AS results have no line for a textbook input, the mapping used is named in `proxies`:
- SG&A (Beneish SGAI): Ind AS results are nature-wise and carry no SG&A; "other expenses" (selling, admin and other
  operating costs) stands in.
- Retained earnings (Altman X2): "other equity" (retained earnings plus other reserves) stands in.
- Cost of goods sold: materials consumed + stock-in-trade purchased + change in inventories. It is zero for most
  service companies; gross-margin signals then carry no information and are marked not applicable.

Variant choices (results filings carry one year-end balance sheet each, so only two are available):
- Piotroski ROA, leverage and asset turnover use YEAR-END total assets (Piotroski used beginning-of-year / average).
- Piotroski "no equity issued" reads the cash-flow line "proceeds from issuing shares" (ESOP exercises excluded).

Not for banks, NBFCs or insurers: their balance sheets are made of financial assets and liabilities, so working
capital, leverage and accrual ratios mean something else. Such companies get None with the reason.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from finresearch.fincalc.numbers import Num, opt_decimal

Facts = Mapping[str, Num | None]

FINANCIAL_BASES = ("interest_earned", "net_premium_income", "premium_earned")  # bank / insurance taxonomies
FINANCIAL_INDUSTRY = re.compile(
    r"\b(bank|banks|banking|nbfc|non[- ]banking|finance|financial|insurance|insurer|housing finance|lending|"
    r"broking|asset management|capital markets)\b", re.I)  # fmt: skip

BENEISH_THRESHOLD = Decimal("-1.78")  # Beneish 1999; some sources use -2.22 [U]
ALTMAN_SAFE, ALTMAN_DISTRESS = Decimal("2.6"), Decimal("1.1")  # on Z'' (without the +3.25 EM constant)
ALTMAN_EM_CONSTANT = Decimal("3.25")
CASH_CONVERSION_FLAG = Decimal("0.6")
ACCRUALS_FLAG = Decimal("0.10")  # rule of thumb for "high"; no Indian threshold has been validated [W]


@dataclass
class Score:
    """One forensic score. `value` is None when it could not be computed; `reason` then says why."""

    key: str
    name: str
    value: Decimal | None
    flag: str | None = None  # plain words: "strong" / "weak", "safe" / "grey" / "distress", "red flag" ...
    red_flag: bool = False
    components: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    proxies: list[str] = field(default_factory=list)
    reason: str | None = None
    thresholds: str = ""
    source: str = ""


def financial_reason(facts: Facts | None = None, *, industry: str | None = None,
                     revenue_basis: str | None = None) -> str | None:  # fmt: skip
    """Why the scores do not apply (a bank, NBFC or insurer), or None when they do."""
    if revenue_basis in FINANCIAL_BASES or (facts and any(facts.get(k) is not None for k in FINANCIAL_BASES)):
        return "a bank or insurer (it files the banking or insurance results taxonomy)"
    if industry and FINANCIAL_INDUSTRY.search(industry):
        return f"a financial company (NSE industry: {industry})"
    return None


# --------------------------------------------------------------------------- helpers
def _get(f: Facts | None, key: str) -> Decimal | None:
    return None if f is None else opt_decimal(f.get(key))


def _need(
    cur: Facts | None, prev: Facts | None, cur_keys: tuple[str, ...], prev_keys: tuple[str, ...] = ()
) -> list[str]:
    missing = [k for k in cur_keys if _get(cur, k) is None]
    missing += [f"{k} (prior year)" for k in prev_keys if _get(prev, k) is None]
    return missing


def _div(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None or b == 0 else a / b


def cogs(f: Facts) -> Decimal | None:
    """Materials consumed + purchases of stock-in-trade + change in inventories (Ind AS nature-wise P&L)."""
    parts = [_get(f, k) for k in ("cost_of_materials", "purchases_stock_in_trade", "changes_in_inventories")]
    if all(p is None for p in parts):
        return None
    return sum((p for p in parts if p is not None), Decimal(0))


def securities(f: Facts) -> Decimal:
    return (_get(f, "current_investments") or Decimal(0)) + (_get(f, "noncurrent_investments") or Decimal(0))


def receivables(f: Facts) -> Decimal | None:
    cur = _get(f, "trade_receivables_current")
    return None if cur is None else cur + (_get(f, "trade_receivables_noncurrent") or Decimal(0))


def net_income(f: Facts) -> Decimal | None:
    return _get(f, "profit_for_period")


def _guard(key: str, name: str, reason: str | None, thresholds: str, source: str) -> Score | None:
    if reason:
        return Score(
            key, name, None, reason=f"Not meaningful for {reason}.", thresholds=thresholds, source=source
        )
    return None


# --------------------------------------------------------------------------- Piotroski
PIOTROSKI_T = "0-9 (here out of the signals that apply): 8-9 strong, 0-2 weak (Piotroski 2000)."


def piotroski(cur: Facts, prev: Facts, *, not_for: str | None = None) -> Score:
    """Piotroski F-score: nine binary signals of profitability, funding and efficiency (one point each).

    ROA>0; CFO>0; ROA rose; CFO>net income (accrual); long-term debt / assets fell; current ratio rose; no shares
    issued for cash; gross margin rose; asset turnover rose. Year-end total assets throughout (see module doc)."""
    src = "Piotroski 2000, JAR 38 (doi:10.2307/2672906)"
    if g := _guard("piotroski", "Piotroski F-score", not_for, PIOTROSKI_T, src):
        return g
    keys = ("profit_for_period", "total_assets", "cfo", "borrowings_noncurrent", "current_assets",
            "current_liabilities", "revenue_from_operations")  # fmt: skip
    missing = _need(cur, prev, (*keys, "proceeds_share_issue"), keys)
    if missing:
        return Score("piotroski", "Piotroski F-score", None, missing=missing, thresholds=PIOTROSKI_T, source=src,
                     reason="Needed lines are not in the filed results.")  # fmt: skip
    ta, ta0 = _get(cur, "total_assets"), _get(prev, "total_assets")
    roa, roa0 = _div(net_income(cur), ta), _div(net_income(prev), ta0)
    cfo = _get(cur, "cfo")
    lev, lev0 = _div(_get(cur, "borrowings_noncurrent"), ta), _div(_get(prev, "borrowings_noncurrent"), ta0)
    cr = _div(_get(cur, "current_assets"), _get(cur, "current_liabilities"))
    cr0 = _div(_get(prev, "current_assets"), _get(prev, "current_liabilities"))
    rev, rev0 = _get(cur, "revenue_from_operations"), _get(prev, "revenue_from_operations")
    at, at0 = _div(rev, ta), _div(rev0, ta0)
    c, c0 = cogs(cur), cogs(prev)
    gm = gm0 = None
    if c is not None and c0 is not None and (c != 0 or c0 != 0):
        gm, gm0 = _div(rev - c, rev), _div(rev0 - c0, rev0)  # type: ignore[operator]
    signals: dict[str, int | None] = {
        "roa_positive": int(roa > 0),  # type: ignore[operator]
        "cfo_positive": int(cfo > 0),  # type: ignore[operator]
        "roa_improved": int(roa > roa0),  # type: ignore[operator]
        "cfo_above_net_income": int(cfo > net_income(cur)),  # type: ignore[operator]
        "leverage_fell": int(lev < lev0),  # type: ignore[operator]
        "current_ratio_rose": None if cr is None or cr0 is None else int(cr > cr0),
        "no_equity_issued": int(_get(cur, "proceeds_share_issue") <= 0),  # type: ignore[operator]
        "gross_margin_rose": None if gm is None or gm0 is None else int(gm > gm0),
        "asset_turnover_rose": None if at is None or at0 is None else int(at > at0),
    }
    applicable = [v for v in signals.values() if v is not None]
    score = Decimal(sum(applicable))
    n = len(applicable)
    na = [k for k, v in signals.items() if v is None]
    flag = "strong" if score >= n - 1 else "weak" if score <= 2 else "middling"
    return Score("piotroski", "Piotroski F-score", score, flag=flag, red_flag=score <= 2,
                 components={**signals, "out_of": n, "not_applicable": na, "roa": roa, "roa_prior": roa0,
                             "current_ratio": cr, "current_ratio_prior": cr0},
                 proxies=["ROA, leverage and asset turnover on year-end total assets"] +
                         (["gross margin not applicable: no cost of goods in the P&L (services)"] if "gross_margin_rose" in na else []),
                 thresholds=PIOTROSKI_T, source=src)  # fmt: skip


# --------------------------------------------------------------------------- Altman
ALTMAN_T = "Z'': above 2.6 safe, 1.1-2.6 grey, below 1.1 distress (Altman 2005). EM score = Z'' + 3.25."


def altman_z_em(cur: Facts, *, not_for: str | None = None) -> Score:
    """Altman Z''-score for emerging-market / non-manufacturing firms:
    Z'' = 6.56 X1 + 3.26 X2 + 6.72 X3 + 1.05 X4 with X1 = working capital / TA, X2 = retained earnings / TA,
    X3 = EBIT / TA, X4 = book equity / total liabilities. The EM score adds 3.25 (rating-equivalent scale)."""
    src = "Altman 2005, Emerging Markets Review 6 (doi:10.1016/j.ememar.2005.09.007)"
    if g := _guard("altman", "Altman Z''-EM", not_for, ALTMAN_T, src):
        return g
    missing = _need(cur, None, ("current_assets", "current_liabilities", "total_assets", "other_equity",
                                "profit_before_tax", "finance_costs", "total_equity", "total_liabilities"))  # fmt: skip
    if missing:
        return Score("altman", "Altman Z''-EM", None, missing=missing, thresholds=ALTMAN_T, source=src,
                     reason="Needed lines are not in the filed results.")  # fmt: skip
    ta = _get(cur, "total_assets")
    x1 = _div(_get(cur, "current_assets") - _get(cur, "current_liabilities"), ta)  # type: ignore[operator]
    x2 = _div(_get(cur, "other_equity"), ta)
    x3 = _div(_get(cur, "profit_before_tax") + _get(cur, "finance_costs"), ta)  # type: ignore[operator]
    x4 = _div(_get(cur, "total_equity"), _get(cur, "total_liabilities"))
    if None in (x1, x2, x3, x4):
        return Score("altman", "Altman Z''-EM", None, thresholds=ALTMAN_T, source=src,
                     reason="Total assets or total liabilities is zero.")  # fmt: skip
    z = Decimal("6.56") * x1 + Decimal("3.26") * x2 + Decimal("6.72") * x3 + Decimal("1.05") * x4  # type: ignore[operator]
    zone = "safe" if z > ALTMAN_SAFE else "distress" if z < ALTMAN_DISTRESS else "grey"
    return Score("altman", "Altman Z''-EM", z, flag=zone, red_flag=zone == "distress",
                 components={"x1_working_capital": x1, "x2_retained_earnings": x2, "x3_ebit": x3,
                             "x4_equity_to_liabilities": x4, "em_score": z + ALTMAN_EM_CONSTANT},
                 proxies=["retained earnings = other equity (includes other reserves)"],
                 thresholds=ALTMAN_T, source=src)  # fmt: skip


# --------------------------------------------------------------------------- Beneish
BENEISH_T = "Above -1.78 flags likely earnings manipulation (Beneish 1999); some use -2.22."
BENEISH_W = {"dsri": Decimal("0.920"), "gmi": Decimal("0.528"), "aqi": Decimal("0.404"), "sgi": Decimal("0.892"),
             "depi": Decimal("0.115"), "sgai": Decimal("-0.172"), "tata": Decimal("4.679"),
             "lvgi": Decimal("-0.327")}  # fmt: skip
BENEISH_CONST = Decimal("-4.84")


def beneish(cur: Facts, prev: Facts, *, not_for: str | None = None) -> Score:
    """Beneish M-score, 8 variables:
    M = -4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI + 0.115 DEPI - 0.172 SGAI + 4.679 TATA - 0.327 LVGI.
    DSRI receivables/sales index; GMI gross-margin index (prior / current); AQI asset-quality index
    (1 - (CA + PP&E + securities)/TA); SGI sales growth; DEPI depreciation-rate index (prior / current);
    SGAI SG&A/sales index; LVGI leverage index ((CL + LTD)/TA); TATA (income from continuing operations - CFO)/TA."""
    src = "Beneish 1999, FAJ 55(5) (doi:10.2469/faj.v55.n5.2296)"
    if g := _guard("beneish", "Beneish M-score", not_for, BENEISH_T, src):
        return g
    both = ("revenue_from_operations", "trade_receivables_current", "current_assets", "ppe", "total_assets",
            "depreciation", "other_expenses", "current_liabilities", "borrowings_noncurrent")  # fmt: skip
    missing = _need(cur, prev, (*both, "cfo"), both)
    if _get(cur, "profit_continuing") is None and _get(cur, "profit_for_period") is None:
        missing.append("profit_continuing")
    if cogs(cur) is None:
        missing.append("cost_of_materials")
    if cogs(prev) is None:
        missing.append("cost_of_materials (prior year)")
    if missing:
        return Score("beneish", "Beneish M-score", None, missing=missing, thresholds=BENEISH_T, source=src,
                     reason="Needed lines are not in the filed results.")  # fmt: skip

    def g(f: Facts, k: str) -> Decimal:
        return _get(f, k)  # type: ignore[return-value]

    s, s0 = g(cur, "revenue_from_operations"), g(prev, "revenue_from_operations")
    ta, ta0 = g(cur, "total_assets"), g(prev, "total_assets")
    gm, gm0 = _div(s - cogs(cur), s), _div(s0 - cogs(prev), s0)  # type: ignore[operator]
    dep, dep0, ppe, ppe0 = g(cur, "depreciation"), g(prev, "depreciation"), g(cur, "ppe"), g(prev, "ppe")

    def ratio(a: Decimal | None, b: Decimal | None) -> Decimal | None:
        return _div(a, b)

    idx = {
        "dsri": ratio(_div(receivables(cur), s), _div(receivables(prev), s0)),
        "gmi": ratio(gm0, gm),
        "aqi": ratio(1 - (g(cur, "current_assets") + ppe + securities(cur)) / ta,
                     1 - (g(prev, "current_assets") + ppe0 + securities(prev)) / ta0),
        "sgi": ratio(s, s0),
        "depi": ratio(_div(dep0, dep0 + ppe0), _div(dep, dep + ppe)),
        "sgai": ratio(_div(g(cur, "other_expenses"), s), _div(g(prev, "other_expenses"), s0)),
        "lvgi": ratio((g(cur, "current_liabilities") + g(cur, "borrowings_noncurrent")) / ta,
                      (g(prev, "current_liabilities") + g(prev, "borrowings_noncurrent")) / ta0),
        "tata": _div((_get(cur, "profit_continuing") or g(cur, "profit_for_period")) - g(cur, "cfo"), ta),
    }  # fmt: skip
    undefined = [k for k, v in idx.items() if v is None]
    if undefined:
        return Score("beneish", "Beneish M-score", None, components=idx, thresholds=BENEISH_T, source=src,
                     reason=f"Undefined index (a zero denominator): {', '.join(undefined)}.")  # fmt: skip
    m = BENEISH_CONST + sum((BENEISH_W[k] * v for k, v in idx.items()), Decimal(0))  # type: ignore[operator]
    red = m > BENEISH_THRESHOLD
    proxies = ["SG&A = other expenses (Ind AS results have no SG&A line)"]
    if cogs(cur) == 0 and cogs(prev) == 0:
        proxies.append("no cost of goods in the P&L (services): GMI is 1 and carries no information")
    return Score("beneish", "Beneish M-score", m, flag="red flag" if red else "no flag", red_flag=red,
                 components=idx, proxies=proxies, thresholds=BENEISH_T, source=src)  # fmt: skip


# --------------------------------------------------------------------------- accruals and cash conversion
ACCRUALS_T = (
    "(Net income - CFO) / average total assets. Lower is better; above +0.10 is flagged (rule of thumb)."
)


def accruals_ratio(cur: Facts, prev: Facts, *, not_for: str | None = None) -> Score:
    """Sloan-style accruals, cash-flow form: (net income - CFO) / average total assets."""
    src = "Sloan 1996, The Accounting Review 71(3)"
    if g := _guard("accruals", "Accruals ratio", not_for, ACCRUALS_T, src):
        return g
    missing = _need(cur, prev, ("profit_for_period", "cfo", "total_assets"), ("total_assets",))
    if missing:
        return Score("accruals", "Accruals ratio", None, missing=missing, thresholds=ACCRUALS_T, source=src,
                     reason="Needed lines are not in the filed results.")  # fmt: skip
    avg = (_get(cur, "total_assets") + _get(prev, "total_assets")) / 2  # type: ignore[operator]
    v = _div(net_income(cur) - _get(cur, "cfo"), avg)  # type: ignore[operator]
    if v is None:
        return Score("accruals", "Accruals ratio", None, reason="Average total assets is zero.", thresholds=ACCRUALS_T,
                     source=src)  # fmt: skip
    red = v > ACCRUALS_FLAG
    return Score("accruals", "Accruals ratio", v, flag="high accruals" if red else "normal", red_flag=red,
                 components={"average_total_assets": avg}, thresholds=ACCRUALS_T, source=src)  # fmt: skip


CFO_T = "CFO / EBITDA summed over the years available. Below 0.6 is flagged (roadmap §C.5)."


def operating_ebitda(f: Facts) -> Decimal | None:
    """PBT before exceptional items + finance costs + depreciation - other income (fincalc.ratios.ebitda)."""
    from finresearch.fincalc.ratios import ebitda

    pbt = _get(f, "profit_before_exceptional_and_tax")
    if pbt is None:
        pbt = _get(f, "profit_before_tax")
    return ebitda(pbt, _get(f, "finance_costs"), _get(f, "depreciation"), _get(f, "other_income") or 0)


def cfo_to_ebitda(years: list[Facts], *, not_for: str | None = None) -> Score:
    """Cash conversion over the years given (oldest first): sum of CFO / sum of operating EBITDA."""
    from finresearch.fincalc.ratios import cfo_to_ebitda as ratio

    src = "fincalc.ratios.cfo_to_ebitda; roadmap §C.5"
    if g := _guard("cfo_ebitda", "CFO / EBITDA", not_for, CFO_T, src):
        return g
    rows = [(_get(y, "cfo"), operating_ebitda(y)) for y in years]
    usable = [(c, e) for c, e in rows if c is not None and e is not None]
    if not usable:
        missing = sorted({k for y in years for k in ("cfo", "profit_before_tax", "finance_costs", "depreciation")
                          if _get(y, k) is None}) or ["cfo"]  # fmt: skip
        return Score("cfo_ebitda", "CFO / EBITDA", None, missing=missing, thresholds=CFO_T, source=src,
                     reason="Needed lines are not in the filed results.")  # fmt: skip
    v = ratio(sum((c for c, _ in usable), Decimal(0)), sum((e for _, e in usable), Decimal(0)))
    if v is None:
        return Score("cfo_ebitda", "CFO / EBITDA", None, reason="EBITDA is zero or negative.", thresholds=CFO_T,
                     source=src)  # fmt: skip
    red = v < CASH_CONVERSION_FLAG
    return Score("cfo_ebitda", "CFO / EBITDA", v, flag="weak cash conversion" if red else "healthy", red_flag=red,
                 components={"years": len(usable), "per_year": [ratio(c, e) for c, e in usable]},
                 thresholds=CFO_T, source=src)  # fmt: skip


def scorecard(cur: Facts | None, prev: Facts | None, *, industry: str | None = None,
              revenue_basis: str | None = None) -> list[Score]:  # fmt: skip
    """All five scores for the latest year (`cur`) against the year before (`prev`)."""
    reason = financial_reason(cur, industry=industry, revenue_basis=revenue_basis)
    c, p = cur or {}, prev or {}
    return [piotroski(c, p, not_for=reason), altman_z_em(c, not_for=reason), beneish(c, p, not_for=reason),
            accruals_ratio(c, p, not_for=reason),
            cfo_to_ebitda([y for y in (prev, cur) if y], not_for=reason)]  # fmt: skip

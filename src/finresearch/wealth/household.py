"""Emergency fund, deposit-insurance concentration, needs-based term cover, EMI/income and prepay-vs-invest.

- Emergency months = liquid money / monthly essential expenses. Liquid = cash and savings + 90 % of FDs/RDs marked
  breakable (a premature-withdrawal penalty haircut, rule of thumb [W]) + other assets marked liquid.
  Target: your own, else 6 months; 9 with dependants or a single earner; 12 with both (planner rules of thumb [W]).
- Deposit insurance (DICGC): "maximum ₹ 5,00,000 (Rupees Five Lakhs)", covering "both principal and interest amount
  held by him in the same right and same capacity", "applied separately to the deposits in each bank"
  (dicgc.org.in/guide-to-deposit-insurance, read 30-Sep-2026). The date the limit took effect is [unverified].
  The flag sums savings and FD/RD balances per bank label; joint accounts in a different capacity are insured
  separately, so it can over-flag. Post-office schemes, EPF and PPF are not bank deposits and are not counted.
- Term cover, needs method: need = PV(annual household expenses over the support years, at the real rate
  (1 + i)/(1 + inflation) - 1, paid at the start of each year) + outstanding loans + gaps on goals marked for cover (today's rupees) - liquid and
  investable assets - existing term cover (floored at 0). Income multiple 10-15x annual income is shown only as a
  cross-check (rule of thumb [W]).
- Health cover: >= ₹10 lakh family floater in metros is a planner rule of thumb [W]; employer group cover is not
  portable.
- EMI / income (FOIR) = total EMIs / net monthly income; lenders commonly cap it around 40-50 % [W].
- Prepay vs invest: after-tax loan rate = rate x (1 - slab x deductible share), where the deductible share is the part
  of this year's interest inside the s.24(b) ₹2,00,000 cap for a self-occupied home under the old regime
  (incometax.gov.in "Individual - business/profession" help page, read 30-Sep-2026); the new regime's disallowance
  for a self-occupied home and the s.80E education-loan deduction are [unverified]. Compared with the post-tax
  expected return on debt (taxed at the slab) and on equity (LTCG 12.5 % ignoring the ₹1.25 lakh exemption, a
  conservative simplification), plus P(equity's annualised return beats the loan rate over the remaining tenure)
  under the lognormal assumption: Phi((ln(1+mu) - sigma^2/2 - ln(1+r_loan)) sqrt(T) / sigma), with mu and sigma
  scaled by (1 - 12.5 %) for tax.
  Prepayment charges: none on floating-rate loans to individuals per RBI [unverified]; check the loan agreement.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

DICGC_LIMIT_INR = 500_000.0
DICGC_SOURCE = "https://www.dicgc.org.in/guide-to-deposit-insurance"
BREAKABLE_HAIRCUT = 0.90
HOME_LOAN_INTEREST_CAP_INR = 200_000.0  # s.24(b), self-occupied, old regime
EQUITY_LTCG_RATE = 0.125  # listed equity / equity MF LTCG from 23-Jul-2024 (roadmap D.7)
FOIR_WARN_PCT = 40.0
HEALTH_RULE_INR = 1_000_000.0
INCOME_MULTIPLE = (10, 15)


def emergency_target(own: float | None, dependants: int, earners: int) -> tuple[float, str]:
    if own is not None:
        return float(own), "your target"
    single = earners <= 1
    if dependants > 0 and single:
        return 12.0, "rule of thumb: 12 months for a single earner with dependants"
    if dependants > 0 or single:
        return 9.0, "rule of thumb: 9 months with dependants or a single earner"
    return 6.0, "rule of thumb: 6 months"


def emergency_months(liquid: float, monthly_expenses: float | None) -> float | None:
    if not monthly_expenses or monthly_expenses <= 0:
        return None
    return liquid / monthly_expenses


def bank_key(label: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (label or "").lower())
    s = re.sub(r"\b(ltd|limited|the|bank)\b", " ", s)
    return " ".join(s.split())


def deposit_concentration(deposits: list[tuple[str | None, float]]) -> list[dict[str, Any]]:
    """Per-bank totals (label normalised: case, punctuation, "Bank"/"Ltd" ignored) with the uninsured excess."""
    by: dict[str, dict[str, Any]] = {}
    for label, value in deposits:
        k = bank_key(label)
        if not k:
            k = "(no bank named)"
        row = by.setdefault(k, {"bank": (label or "(no bank named)").strip(), "total": 0.0, "count": 0})
        row["total"] += value
        row["count"] += 1
    out = []
    for r in by.values():
        r["over_limit"] = r["total"] > DICGC_LIMIT_INR and r["bank"] != "(no bank named)"
        r["uninsured"] = max(0.0, r["total"] - DICGC_LIMIT_INR) if r["over_limit"] else 0.0
        out.append(r)
    return sorted(out, key=lambda r: -r["total"])


def real_rate(nominal_pct: float, inflation_pct: float) -> float:
    return (1 + nominal_pct / 100) / (1 + inflation_pct / 100) - 1


def pv_annuity_due(amount: float, years: int, rate: float) -> float:
    if years <= 0:
        return 0.0
    if abs(rate) < 1e-12:
        return amount * years
    return amount * (1 - (1 + rate) ** -years) / rate * (1 + rate)


@dataclass(frozen=True)
class CoverNeed:
    expenses_pv: float
    loans: float
    goal_gaps: float
    assets: float
    existing: float
    years: int
    rate: float

    @property
    def need(self) -> float:
        return self.expenses_pv + self.loans + self.goal_gaps

    @property
    def gap(self) -> float:
        return max(0.0, self.need - self.assets - self.existing)


def term_cover(
    *,
    annual_expenses: float,
    years: int,
    nominal_pct: float,
    inflation_pct: float,
    loans: float,
    goal_gaps: float,
    assets: float,
    existing: float,
) -> CoverNeed:
    r = real_rate(nominal_pct, inflation_pct)
    return CoverNeed(pv_annuity_due(annual_expenses, years, r), loans, goal_gaps, assets, existing, years, r)


def foir_pct(total_emi: float, monthly_income: float | None) -> float | None:
    if not monthly_income or monthly_income <= 0:
        return None
    return total_emi / monthly_income * 100


def deductible_share(kind: str, regime: str, annual_interest: float) -> tuple[float, str]:
    """Share of this year's interest that reduces taxable income, and why."""
    if kind == "home":
        if regime != "old":
            return 0.0, "new regime: no deduction for a self-occupied home loan's interest [unverified]"
        if annual_interest <= 0:
            return 0.0, "no interest this year"
        share = min(1.0, HOME_LOAN_INTEREST_CAP_INR / annual_interest)
        return share, "old regime: s.24(b) interest deduction up to ₹2,00,000 a year (self-occupied)"
    if kind == "education":
        if regime != "old":
            return 0.0, "new regime: s.80E not available [unverified]"
        return 1.0, "old regime: s.80E interest deduction, no cap [unverified]"
    return 0.0, "no tax deduction for this loan type"


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def p_equity_beats(rate_pct: float, mu_pct: float, sigma_pct: float, years: float) -> float | None:
    """P(annualised equity return over `years` > rate) under the lognormal with E[growth] = 1 + mu."""
    if years <= 0 or sigma_pct <= 0:
        return None
    s = sigma_pct / 100
    m = math.log1p(mu_pct / 100) - s * s / 2
    return normal_cdf((m - math.log1p(rate_pct / 100)) * math.sqrt(years) / s)


def prepay_vs_invest(
    *,
    kind: str,
    rate_pct: float,
    balance: float,
    regime: str,
    slab_pct: float,
    equity_mu_pct: float,
    equity_sigma_pct: float,
    debt_mu_pct: float,
    years_left: float,
) -> dict[str, Any]:
    interest = balance * rate_pct / 100
    share, why = deductible_share(kind, regime, interest)
    after_tax_loan = rate_pct * (1 - slab_pct / 100 * share)
    debt_post = debt_mu_pct * (1 - slab_pct / 100)
    eq_post = equity_mu_pct * (1 - EQUITY_LTCG_RATE)
    # post-tax equity: LTCG scales gains, so both the mean and the spread shrink by (1 - 12.5 %) (approximation)
    p_beat = p_equity_beats(after_tax_loan, eq_post, equity_sigma_pct * (1 - EQUITY_LTCG_RATE), years_left)
    if after_tax_loan >= eq_post:
        reading = (
            "The numbers imply prepaying earns more than the assumed post-tax equity return, and it is "
            "certain; investing does not."
        )
    elif after_tax_loan >= debt_post:
        reading = (
            "The numbers imply prepaying beats the assumed post-tax debt return with certainty; the assumed "
            "equity return is higher but uncertain."
        )
    else:
        reading = (
            "The numbers imply the assumed post-tax debt and equity returns both exceed the after-tax loan "
            "rate; that comparison assumes returns you may not get."
        )
    return {
        "after_tax_loan_pct": after_tax_loan,
        "deductible_share": share,
        "deduction_note": why,
        "debt_post_tax_pct": debt_post,
        "equity_post_tax_pct": eq_post,
        "p_equity_beats_loan": p_beat,
        "years_left": years_left,
        "reading": reading,
    }

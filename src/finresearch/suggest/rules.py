"""Deterministic inputs for a suggestion: live metrics, rule evaluation and lot limits."""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import Claim, Company, IpoOffer, ResearchRun
from finresearch.suggest.profile import Profile, Rule

RETAIL_CAP_INR = Decimal(200_000)  # SEBI: retail individual bids up to ₹2 lakh
SHNI_CAP_INR = Decimal(1_000_000)  # small NII: above ₹2 lakh up to ₹10 lakh
OPS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}
CATEGORY_NAMES = {"qib_times": ("qualified institutional", "qib"), "nii_times": ("non institutional", "nii"),
                  "rii_times": ("retail individual", "rii")}  # fmt: skip


def dec_str(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f")


@dataclass
class Metric:
    value: Decimal | None
    source: str
    as_of: str | None = None


@dataclass
class RuleResult:
    rule: Rule
    status: str  # fired | clear | unknown
    value: Decimal | None = None
    source: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"rule": self.rule.model_dump(mode="json"), "status": self.status,
                "value": dec_str(self.value), "source": self.source}  # fmt: skip


@dataclass
class Inputs:
    metrics: dict[str, Metric] = field(default_factory=dict)
    rules: list[RuleResult] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"metrics": {k: {"value": dec_str(m.value), "source": m.source,
                                "as_of": m.as_of} for k, m in self.metrics.items()},
                "rules": [r.to_json() for r in self.rules]}  # fmt: skip


def subscription_metrics(detail) -> dict[str, Metric]:
    """QIB / NII / RII / total times from NSE's combined (NSE+BSE) table."""
    snap = detail.combined if detail is not None else None
    if snap is None:
        return {}
    as_of = snap.as_of.isoformat() if snap.as_of else None
    out = {"total_times": Metric(snap.total_times, "NSE combined subscription", as_of)}
    for key, names in CATEGORY_NAMES.items():
        row = next((c for c in snap.top_level if any(n in c.name.lower() for n in names)), None)
        if row is not None:
            out[key] = Metric(row.times, "NSE combined subscription", as_of)
    return out


def _ledger_fact(session: Session, run_id: int, metric: str) -> Decimal | None:
    c = session.scalars(select(Claim).where(Claim.run_id == run_id, Claim.metric == metric,
                                            Claim.status == "verified").order_by(Claim.id)).first()  # fmt: skip
    return c.value if c else None


def gather(session: Session, run_id: int, profile: Profile, *, live_detail=None, now: datetime,
           gate_ok: bool | None) -> Inputs:  # fmt: skip
    inputs = Inputs()
    m = inputs.metrics
    m.update(subscription_metrics(live_detail))
    run = session.get(ResearchRun, run_id)
    issue_info = ((run.manifest or {}).get("facts") or {}).get("issue_info") or {} if run else {}
    for key, fallback in (("price_band_upper", _upper_from(issue_info)), ("lot_size", _lot_from(issue_info))):
        v = _ledger_fact(session, run_id, key)
        m[key] = (Metric(v, "ledger (verified claim)") if v is not None
                  else Metric(fallback, "NSE issue information (run facts)") if fallback is not None
                  else Metric(None, "not in the ledger or the NSE issue information"))  # fmt: skip
    upper, lot = m["price_band_upper"].value, m["lot_size"].value
    if upper and lot:
        cost = upper * lot
        m["lot_cost"] = Metric(cost, "fincalc: lot_size x price_band_upper")
        m["max_lots_by_capital"] = Metric(Decimal(int(profile.capital_per_ipo_inr // cost)),
                                          "fincalc: floor(capital / lot_cost)")  # fmt: skip
    else:
        m["lot_cost"] = Metric(None, "lot size or price band unknown")
        m["max_lots_by_capital"] = Metric(None, "lot size or price band unknown")
    offer = session.scalars(select(IpoOffer).where(IpoOffer.company_id == run.company_id)
                            .order_by(IpoOffer.id.desc())).first() if run and run.company_id else None  # fmt: skip
    close = offer.close_date if offer else None
    if close is None and run is not None:
        close = _close_from_facts(run)
    m["bidding_days_left"] = Metric(
        Decimal(bidding_days_left(now.date(), close)) if close else None,
        "fincalc: exchange bidding days from today to the close" if close else "close date unknown",
    )
    m["gate_ok"] = Metric(None if gate_ok is None else Decimal(int(gate_ok)), "publish gate")
    inputs.rules = [evaluate(r, m) for r in profile.rules]
    return inputs


def _upper_from(issue_info: dict[str, str]) -> Decimal | None:
    from finresearch.adapters.nse import parse_price_band

    return parse_price_band(issue_info.get("Price Range"))[1]


def _lot_from(issue_info: dict[str, str]) -> Decimal | None:
    import re

    m = re.search(r"(\d[\d,]*)\s*equity shares", issue_info.get("Bid Lot") or "", re.I)
    return Decimal(m.group(1).replace(",", "")) if m else None


def _close_from_facts(run: ResearchRun):
    from datetime import date

    raw = ((run.manifest or {}).get("facts") or {}).get("issue_close")
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def bidding_days_left(today, close) -> int:
    """Exchange business days from today to the close, both included (0 once the issue has closed)."""
    from finresearch.fincalc.dates import business_days_between, is_business_day

    if close < today:
        return 0
    return business_days_between(today, close) + (1 if is_business_day(today) else 0)


def evaluate(rule: Rule, metrics: dict[str, Metric]) -> RuleResult:
    m = metrics.get(rule.metric)
    if m is None or m.value is None:
        return RuleResult(rule, "unknown", None, m.source if m else "not available")
    fired = OPS[rule.op](m.value, rule.value)
    return RuleResult(rule, "fired" if fired else "clear", m.value, m.source)


def lot_limits(profile: Profile, lot_cost: Decimal | None) -> dict[str, int | None]:
    """Maximum lots by capital and by the SEBI category cap for the profile's category."""
    if not lot_cost:
        return {"by_capital": None, "by_category": None, "min_lots": None}
    by_capital = int(profile.capital_per_ipo_inr // lot_cost)
    if profile.category == "retail":
        by_cat, min_lots = int(RETAIL_CAP_INR // lot_cost), 1
    elif profile.category == "shni":
        by_cat, min_lots = int(SHNI_CAP_INR // lot_cost), int(RETAIL_CAP_INR // lot_cost) + 1
    else:
        by_cat, min_lots = None, int(SHNI_CAP_INR // lot_cost) + 1
    return {"by_capital": by_capital, "by_category": by_cat, "min_lots": min_lots}


def company_of(session: Session, run_id: int) -> Company | None:
    run = session.get(ResearchRun, run_id)
    return session.get(Company, run.company_id) if run and run.company_id else None

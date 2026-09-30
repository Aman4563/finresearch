"""The personal tax calendar: advance-tax instalments and other dated deadlines, as a dated rule table.

Advance tax (individuals, not under presumptive taxation) is paid in four instalments by 15-Jun, 15-Sep, 15-Dec and
15-Mar of the financial year: at least 15 %, 45 %, 75 % and 100 % of the year's tax, cumulatively. It is due only
when the year's tax (after TDS) is ₹10,000 or more. Interest for deferring an instalment (old s.234C) is not charged
on the shortfall caused by capital gains or dividends when the tax on them is paid in the instalments due after the
gain or dividend arises (or by 31-Mar).

[unverified] These are long-standing rules (Income-tax Act 1961 ss.208, 211, 234C; the Income-tax Act 2025
renumbered the sections for tax year 2026-27 onward). They could not be re-read from an official page on
30-Sep-2026: incometax.gov.in help pages (return-applicable-1, individual-business-profession) do not carry them,
/iec/foportal/help/e-pay-tax-advance-tax returned 404, the e-Filing tax calendar
(eportal.incometax.gov.in/iec/foservices/#/TaxCalc/calender) is a JavaScript-only page, and
incometaxindia.gov.in returned 403 / connection reset. Verify each year, and with a chartered accountant.

The ITR due date (31-Jul for individuals whose accounts need no audit) is also [unverified]. The financial year's
last day (31-Mar) is certain: gains and losses booked after it fall into the next year.

The estimator is keyed on rules, not section numbers (roadmap D.7), uses Decimal, and only covers what the app
knows: capital gains realised this year and dividends recorded this year. Salary, interest, TDS already deducted and
any advance tax already paid are unknown to the app, so the output is "the part of the schedule these gains and
dividends account for", never a demand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

UNVERIFIED = (
    "[unverified] Advance-tax dates, percentages and the ₹10,000 threshold are long-standing rules that could "
    "not be re-read from incometax.gov.in on 30-Sep-2026; verify them each year."
)
CESS = Decimal("0.04")


@dataclass(frozen=True)
class Instalment:
    due: date
    cumulative_pct: Decimal  # share of the year's tax paid by this date, cumulatively (0.15 = 15 %)
    label: str


@dataclass(frozen=True)
class AdvanceTaxRule:
    """One dated version of the schedule. `fy` is named by its end year (2027 = FY 2026-27)."""

    first_fy: int
    last_fy: int | None
    schedule: tuple[
        tuple[int, int, Decimal], ...
    ]  # (month, day, cumulative share); months 6..12 in the FY's first
    #                                                 calendar year, 1..3 in the second
    threshold_inr: Decimal
    status: str
    note: str


ADVANCE_TAX_RULES: tuple[AdvanceTaxRule, ...] = (
    AdvanceTaxRule(
        first_fy=2017,  # the four-instalment schedule for all non-corporate taxpayers from FY 2016-17
        last_fy=None,
        schedule=(
            (6, 15, Decimal("0.15")),
            (9, 15, Decimal("0.45")),
            (12, 15, Decimal("0.75")),
            (3, 15, Decimal("1.00")),
        ),
        threshold_inr=Decimal(10_000),
        status="unverified",
        note=UNVERIFIED,
    ),
)


def rule_for_fy(fy: int) -> AdvanceTaxRule | None:
    return next(
        (r for r in ADVANCE_TAX_RULES if r.first_fy <= fy and (r.last_fy is None or fy <= r.last_fy)), None
    )


def instalments(fy: int) -> list[Instalment]:
    """The instalment dates of financial year `fy` (named by its end year)."""
    rule = rule_for_fy(fy)
    if rule is None:
        return []
    out = []
    for month, day, share in rule.schedule:
        year = fy - 1 if month >= 4 else fy
        d = date(year, month, day)
        out.append(Instalment(d, share, f"{int(share * 100)} % by {d:%d-%b-%Y}"))
    return out


def next_instalment(today: date, fy: int) -> Instalment | None:
    """The first instalment on or after `today` in financial year `fy` (None after 15-Mar)."""
    return next((i for i in instalments(fy) if i.due >= today), None)


def _money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def advance_tax_estimate(capital_gains_tax: Decimal, dividends: Decimal, slab_rate: Decimal, today: date,
                         fy: int) -> dict[str, Any]:  # fmt: skip
    """The advance tax that this year's realised capital gains and dividends account for.

    - capital_gains_tax: the year's tax on realised gains incl. cess (fincalc.tax.fy_tax(...).total);
    - dividends: dividends recorded this year, taxed at the slab rate plus 4 % cess;
    - total = both; below the threshold (₹10,000) nothing is due from these alone;
    - by the next instalment the schedule asks for cumulative_pct × total. Because tax on capital gains and
      dividends may be paid in the instalments after they arise without deferment interest, the amount is the
      *schedule's* figure, shown as a planning aid (the app cannot see tax already paid or TDS).
    """
    rule = rule_for_fy(fy)
    div_tax = _money(dividends * slab_rate * (1 + CESS)) if dividends > 0 else Decimal(0)
    total = _money(capital_gains_tax) + div_tax
    nxt = next_instalment(today, fy)
    sched = [{"due": i.due.isoformat(), "cumulative_pct": float(i.cumulative_pct * 100), "label": i.label,
              "amount": float(_money(total * i.cumulative_pct)), "past": i.due < today} for i in instalments(fy)]  # fmt: skip
    threshold = rule.threshold_inr if rule else Decimal(10_000)
    below = total < threshold
    due_next = Decimal(0) if below or nxt is None else _money(total * nxt.cumulative_pct)
    return {
        "fy": fy,
        "capital_gains_tax": float(_money(capital_gains_tax)),
        "dividends": float(dividends),
        "dividend_tax": float(div_tax),
        "total": float(total),
        "threshold": float(threshold),
        "below_threshold": below,
        "next": None if nxt is None else {"due": nxt.due.isoformat(), "cumulative_pct": float(nxt.cumulative_pct * 100),
                                          "amount": float(due_next), "days": (nxt.due - today).days},
        "schedule": sched,
        "status": rule.status if rule else "unverified",
        "note": UNVERIFIED,
        "caveats": [
            "Only capital gains realised in the app and dividends recorded in it are counted: salary, interest, "
            "TDS and advance tax already paid are unknown here.",
            "Tax on a capital gain or a dividend may be paid in the instalments due after it arises without "
            "deferment interest [unverified].",
            "Surcharge and rebates are not modelled.",
        ],
    }  # fmt: skip


def calendar(today: date, fy: int, *, horizon_days: int = 120) -> list[dict[str, Any]]:
    """Dated tax deadlines from `today` to `today + horizon_days`: advance-tax instalments (this and next FY), the
    financial year's end (last day to book gains or losses in it) and the ITR due date [unverified]."""
    items: list[dict[str, Any]] = []
    for y in (fy, fy + 1):
        for i in instalments(y):
            items.append({"day": i.due.isoformat(), "kind": "advance_tax", "title": f"Advance tax: {i.label}",
                          "verified": False, "note": UNVERIFIED})  # fmt: skip
        items.append({"day": date(y, 3, 31).isoformat(), "kind": "fy_end",
                      "title": f"Financial year {y - 1}-{y % 100:02d} ends: last day to book gains or losses in it",
                      "verified": True, "note": "Trades settle T+1: a sale must execute by the last trading day."})  # fmt: skip
        items.append({"day": date(y, 7, 31).isoformat(), "kind": "itr",
                      "title": f"Income-tax return due for FY {y - 1}-{y % 100:02d} (no audit)",
                      "verified": False, "note": "[unverified] 31-Jul for individuals whose accounts need no audit; "
                      "the date is sometimes extended."})  # fmt: skip
    end = date.fromordinal(today.toordinal() + horizon_days)
    return sorted(
        (x for x in items if today.isoformat() <= x["day"] <= end.isoformat()), key=lambda x: x["day"]
    )

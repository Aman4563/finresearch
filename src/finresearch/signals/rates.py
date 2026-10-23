"""The risk-free rate for option maths and fund and portfolio metrics (#479, the owner's decision of 23-Oct-2026).

The chain is fincalc.riskfree: the owner's entered yield (table `owner_rate`, refseries.owner_rates), else the RBI
repo-rate proxy (the repo rate in force on the day from fincalc.policy_rates' hand-typed table, plus a stated
spread), else unknown before 01-Jan-2015. Nothing is fetched from anyone: FBIL is paused (#476: its Terms of Use of
13-Oct-2026 forbid a database or feed built from its free site) and RBI's pages are not collected. There is no fixed
fallback rate any more: the earlier 6.5 % and FBIL's dated 23-Sep-2026 curve are gone.

Every answer is a dict: "known", "rate" (continuously compounded, a fraction; None when unknown), "yield" (simple,
Actual/365, as RBI quotes it), "tenor_days", "tenor_years", "tier" ("owner" | "proxy" | None), "as_of", "source",
"note", "url", "shift_bp", "basis", "par_yield" (None: there is no par curve) and "fallback" (False: the proxy is the
designed source, not a fallback).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from finresearch.fincalc import riskfree as R

# () -> the owner's entries; None = read the owner_rate table. conftest sets it to `list`, so a test without the
# test database never reads the configured one.
OWNER: Callable[[], list[R.OwnerRate]] | None = None
# () -> today (IST) for risk_free(); None = fincalc.dates.today_ist. conftest pins it so live-rate tests do not depend
# on the day they run.
TODAY: Callable[[], date] | None = None


def _entries(s: Any = None) -> tuple[list[R.OwnerRate], str | None]:
    """The owner's entries (in the caller's session when given), and why they are missing if they could not be
    read: the chain then answers from the policy-rate table, which needs no database."""
    if OWNER is not None:
        return list(OWNER()), None
    from finresearch.refseries import owner_rates

    try:
        if s is None:
            from finresearch.db import session_scope

            with session_scope() as own:
                return owner_rates.entries(own), None
        with s.begin_nested():  # a failed read must not break the caller's transaction
            return owner_rates.entries(s), None
    except Exception as e:
        return (
            [],
            f"the owner-entered rates could not be read ({type(e).__name__}): the RBI repo-rate proxy is used",
        )


def _answer(day: date, t_years: float, *, before: bool = False, s: Any = None) -> dict[str, Any]:
    entries, why = _entries(s)
    out = R.answer(day, t_years, entries, before=before)
    if why:
        out["note"] = f"{out['note']}; {why}"
    return out


async def risk_free(t_years: float) -> dict[str, Any]:
    """Today's (IST) rate for a term of `t_years` (see the module docstring)."""
    from finresearch.fincalc.dates import today_ist

    return _answer((TODAY or today_ist)(), max(float(t_years), 0.0))


async def risk_free_on(day: date, t_years: float, *, before: bool = False) -> dict[str, Any]:
    """The rate for `day` (`before`: only what was known at the start of the day)."""
    return _answer(day, t_years, before=before)


def rate_on(s: Any, day: date, t_years: float, *, before: bool = False) -> dict[str, Any]:
    """risk_free_on in the caller's session (the owner's entries are read in it)."""
    return _answer(day, t_years, before=before, s=s)


async def rate_for(day: date, t_years: float, *, before: bool = False) -> dict[str, Any]:
    """risk_free_on with its own session."""
    return _answer(day, t_years, before=before)


def given(rate: float, why: str) -> dict[str, Any]:
    """A rate a caller fixed on purpose (a registered setting, or a value the user typed), labelled as such."""
    return {"known": True, "rate": rate, "yield": None, "par_yield": None, "tenor_days": None, "tenor_years": None,
            "as_of": None, "tier": "given", "fallback": False, "source": why, "note": why, "url": None,
            "shift_bp": None, "basis": "as given"}  # fmt: skip


def user_rate(rate: float) -> dict[str, Any]:
    return given(rate, "your input") | {"note": "the risk-free rate you supplied"}


def label(info: dict[str, Any]) -> str:
    """One line naming the rate used: 'RBI repo-rate proxy: repo 5.50 % in force from 07-Oct-2026: 5.46%
    continuous', 'owner-entered rate: 91D for 14-Oct-2026: 5.69% continuous', or 'unknown: <reason>'."""
    if info.get("rate") is None:
        return f"unknown: {info.get('note')}"
    return f"{info.get('source')}: {float(info['rate']):.2%} continuous"


def effective_annual(info: dict[str, Any]) -> float | None:
    """The answer as an effective annual rate, (1 + y D/365)^(365/D) - 1 from its simple yield over its own term
    (fincalc.riskfree); a given rate is taken as already annual; None when unknown."""
    if info.get("rate") is None:
        return None
    if info.get("yield") is None or not info.get("tenor_days"):
        return float(info["rate"])
    return R.effective_annual(float(info["yield"]), int(info["tenor_days"]))


# --------------------------------------------------------------------------- fund and portfolio metrics (#479)
SOURCE_LABEL = "owner-entered rates, else the RBI repo-rate proxy"
MONTHLY_LABEL = ("each month's own risk-free rate: the month's average 91-day rate (owner-entered, else the RBI "
                 "repo-rate proxy), accrued day by day as (1 + y x 91/365)^(days/91) - 1")  # fmt: skip
FACTSHEET_LABEL = ("factsheet-style: one overnight rate, the RBI repo rate on the last working day, applied to every "
                   "month, as Indian AMC factsheets apply one overnight MIBOR/TREPS print")  # fmt: skip


def months_for(start: date, end: date, *, s: Any = None) -> tuple[dict[date, R.MonthRate], str | None]:
    """fincalc.riskfree.month_rates for [start, end] with the owner's entries, and why they are missing."""
    entries, why = _entries(s)
    return R.month_rates(start, end, entries), why


def accrual_fn(months: dict[date, R.MonthRate]) -> Callable[[date, date], float | None]:
    """(d0, d1) -> the risk-free return over (d0, d1] (fincalc.riskfree.accrual), remembered per period."""
    seen: dict[tuple[date, date], float | None] = {}

    def rf(d0: date, d1: date) -> float | None:
        if (d0, d1) not in seen:
            seen[(d0, d1)] = R.accrual(d0, d1, months)
        return seen[(d0, d1)]

    return rf


def monthly_json(months: dict[date, R.MonthRate], why: str | None = None) -> dict[str, Any]:
    """What the pages show about a monthly series: each month's average 91-day yield and the days per tier."""
    tiers: dict[str, int] = {}
    for m in months.values():
        for t, n in m.tiers:
            tiers[t] = tiers.get(t, 0) + n
    return {"method": "monthly", "label": MONTHLY_LABEL, "basis": R.BASIS, "tiers": tiers, "note": why,
            "months": [{"month": f"{m.month:%Y-%m}", "days": m.days, "y91": m.y91, "tiers": dict(m.tiers)}
                       for m in months.values() if m.y91 is not None],
            "unknown": [{"month": f"{m.month:%Y-%m}", "note": m.note} for m in months.values() if m.y91 is None]}  # fmt: skip


def factsheet(day: date) -> dict[str, Any]:
    """The factsheet-style rate: the repo rate in force on `day` (the window's last working day) as one annual rate
    for every period, the way Indian AMC factsheets apply one overnight rate of the factsheet date."""
    from finresearch.fincalc import policy_rates as P

    row = P.in_force(day)
    if row is None:
        return {"known": False, "rate": None, "as_of": day.isoformat(), "label": FACTSHEET_LABEL, "url": None,
                "source": None, "note": R.unknown(day, 1)["note"]}  # fmt: skip
    return {"known": True, "rate": float(row.repo), "as_of": day.isoformat(), "label": FACTSHEET_LABEL,
            "source": f"RBI repo rate {float(row.repo) * 100:.2f} % in force on {day:%d-%b-%Y} (from "
                      f"{row.effective:%d-%b-%Y})", "url": row.url, "note": None}  # fmt: skip

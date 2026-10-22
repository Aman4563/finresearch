"""Dated risk-free rates from the stored FBIL series (#366, refseries.rates). Every answer says which curve and day it
used; a date with no usable curve is unknown (`rate` None, `known` False, `note` says why), never a fixed default.

* par_rate_on: the G-sec par yield at a tenor (linear between FBIL's tenors, flat beyond its ends, as
  adapters.fbil.ParCurve.par_yield), converted to continuous r = 2 ln(1 + y/2) (fincalc.refseries.short_rate_proxy,
  the same conversion signals.rates applies to today's curve).
* tbill_rate_on: one T-bill benchmark tenor. FBIL's page does not state the quotation basis; it is ASSUMED to be
  RBI's T-bill yield convention, a simple annual yield on Actual/365 (y = (100 - P)/P x 365/d), so the continuous
  rate is ln(1 + y d/365) x 365/d. The answer carries that assumption in `basis`.

`before=True` uses only curves published before `day` (FBIL publishes the G-sec curve and T-bills in the evening, so a
decision taken during the session must not read that day's curve). A curve older than MAX_AGE_DAYS calendar days is
not used: it covers a weekend plus a holiday, not a gap in the data.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import RateCurve

MAX_AGE_DAYS = 5
SHORTEST_TENOR = 0.25  # FBIL's first par-yield tenor (years)


def _unknown(note: str, **extra: Any) -> dict[str, Any]:
    return {"known": False, "rate": None, "as_of": None, "source": None, "note": note} | extra


def _latest_day(s: Session, series: str, day: date, before: bool) -> date | None:
    cond = RateCurve.day < day if before else RateCurve.day <= day
    return s.scalar(select(func.max(RateCurve.day)).where(RateCurve.series == series, cond))


def _usable(
    s: Session, series: str, day: date, before: bool, max_age_days: int
) -> tuple[date | None, str | None]:
    got = _latest_day(s, series, day, before)
    if got is None:
        return None, f"no FBIL {series} curve stored {'before' if before else 'on or before'} {day}"
    if (day - got).days > max_age_days:
        return None, f"the latest FBIL {series} curve ({got}) is older than {max_age_days} days on {day}"
    return got, None


def par_rate_on(s: Session, day: date, t_years: float, *, before: bool = False,
                max_age_days: int = MAX_AGE_DAYS) -> dict[str, Any]:  # fmt: skip
    """{"known", "rate" (continuous), "par_yield" (half-yearly), "tenor_years", "as_of", "source", "note"}."""
    from finresearch.adapters.fbil import ParCurve, ParPoint
    from finresearch.fincalc.refseries import short_rate_proxy

    as_of, why = _usable(s, "gsec_par", day, before, max_age_days)
    if as_of is None:
        return _unknown(why or "", par_yield=None, tenor_years=None)
    pts = s.scalars(select(RateCurve).where(RateCurve.series == "gsec_par", RateCurve.day == as_of)).all()
    curve = ParCurve(as_of=as_of, points=[ParPoint(p.tenor_years, p.rate, p.annualised) for p in pts])
    tenor = max(float(t_years), SHORTEST_TENOR)
    y = float(curve.par_yield(tenor))
    note = f"{y:.2%} half-yearly -> {short_rate_proxy(y):.2%} continuous"
    if t_years < SHORTEST_TENOR:
        note += f"; under {SHORTEST_TENOR:g} years, FBIL's shortest tenor, so that point is used"
    return {"known": True, "rate": short_rate_proxy(y), "par_yield": y, "tenor_years": tenor,
            "as_of": as_of.isoformat(), "note": note,
            "source": f"FBIL G-sec par yield, {tenor:g}-year point, curve of {as_of.isoformat()}"}  # fmt: skip


TBILL_BASIS = "assumed simple yield, Actual/365 (RBI's T-bill convention; FBIL's page does not state it)"


def tbill_rate_on(s: Session, day: date, tenor: str = "3M", *, before: bool = False,
                  max_age_days: int = MAX_AGE_DAYS) -> dict[str, Any]:  # fmt: skip
    """{"known", "rate" (continuous), "yield" (as published), "days", "as_of", "basis", "source", "note"}."""
    as_of, why = _usable(s, "tbill", day, before, max_age_days)
    row = None
    if as_of is not None:
        row = s.scalar(select(RateCurve).where(RateCurve.series == "tbill", RateCurve.day == as_of,
                                               RateCurve.tenor == tenor))  # fmt: skip
        why = None if row is not None else f"FBIL published no {tenor} T-bill rate on {as_of}"
    if row is None:
        return _unknown(why or "", **{"yield": None, "days": None, "basis": TBILL_BASIS})
    y, d = float(row.rate), round(float(row.tenor_years) * 365)
    return {"known": True, "rate": math.log1p(y * d / 365) * 365 / d, "yield": y, "days": d,
            "as_of": as_of.isoformat(), "basis": TBILL_BASIS, "note": f"{tenor} T-bill {y:.2%}",
            "source": f"FBIL T-bill benchmark, {tenor}, {as_of.isoformat()}"}  # fmt: skip

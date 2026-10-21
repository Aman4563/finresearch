"""CPI inflation for the goal planner (#347): one monthly index per category from MoSPI's two CPI series, and the
trailing / long-run rates and year-to-year spread the Monte Carlo uses.

Splicing. MoSPI moved the CPI to base 2024=100 from the January-2026 index (PIB, 12-Feb-2026); the 2012=100 series
ends at December 2025. The two bases are not linked here with a factor of our own: each 2024-base month extends the
2012-base index by MoSPI's own published year-on-year rate for that month, L(m) = L(m - 12) x (1 + yoy(m) / 100), so
every rate read off the chained index from 2026 on is exactly an official one. The chain stops at the first missing
month (a gap is never bridged, which would stretch the window silently).

Rates (all from the chained index, simple annual %):
- trailing: the latest 12 months, L(T) / L(T - 12) - 1 (MoSPI's latest headline year-on-year rate).
- long-run: the geometric mean over `years` whole years, (L(T) / L(T - 12 years))^(1 / years) - 1.
- spread: the sample standard deviation of the `years` non-overlapping annual rates ending at T; the Monte Carlo
  draws each year's inflation around the long-run rate with it.
A history shorter than `years` leaves long-run and spread unknown (None) with the reason; never a default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from finresearch.adapters.mospi_cpi import SOURCE, CpiData


def _shift(d: date, months: int) -> date:
    k = d.year * 12 + d.month - 1 + months
    return date(k // 12, k % 12 + 1, 1)


def chain(old: dict[date, float], new_yoy: dict[date, float]) -> dict[date, float]:
    """The 2012-base index levels, extended month by month by the newer series' official year-on-year rates."""
    out = dict(old)
    if not out:
        return out
    m = _shift(max(out), 1)
    while m in new_yoy and _shift(m, -12) in out:
        out[m] = out[_shift(m, -12)] * (1 + new_yoy[m] / 100)
        m = _shift(m, 1)
    return out


@dataclass(frozen=True)
class CpiStats:
    as_of: date | None  # the latest month of the chained index
    trailing_pct: float | None
    long_run_pct: float | None
    sd_pct: float | None
    years: int
    start: date | None  # the first month of the long-run window
    why: str | None = None  # why a figure is unknown


def stats(levels: dict[date, float], years: int = 10) -> CpiStats:
    if not levels:
        return CpiStats(None, None, None, None, years, None, "no CPI index months")
    t = max(levels)
    yr = [levels.get(_shift(t, -12 * k)) for k in range(years + 1)]
    trailing = None if yr[1] is None else (levels[t] / yr[1] - 1) * 100
    if any(v is None for v in yr):
        have = next(k for k, v in enumerate(yr) if v is None) - 1
        return CpiStats(t, trailing, None, None, years, None,
                        f"the CPI history covers {max(have, 0)} whole years to {t:%b-%Y}; the long-run rate needs "
                        f"{years} years")  # fmt: skip
    annual = [(yr[k] / yr[k + 1] - 1) * 100 for k in range(years)]  # type: ignore[operator]
    long_run = ((yr[0] / yr[years]) ** (1 / years) - 1) * 100  # type: ignore[operator]
    mean = sum(annual) / years
    sd = math.sqrt(sum((a - mean) ** 2 for a in annual) / (years - 1)) if years > 1 else None
    return CpiStats(t, trailing, long_run, sd, years, _shift(t, -12 * years))


# --------------------------------------------------------------------------- a goal's inflation basis
BASES = ("manual", "cpi_general", "cpi_education", "cpi_health")
LONG_RUN_YEARS = 10
SENSITIVITY_PP = 2.0
_CATEGORIES = ("general", "education", "health")


@dataclass(frozen=True)
class CpiSummary:
    stats: dict[str, CpiStats]  # category -> rates
    vintage: dict[str, Any]


def summarize(data: CpiData, years: int = LONG_RUN_YEARS) -> CpiSummary:
    """Per-category rates from the fetched MoSPI series, and the vintage they rest on."""
    st = {c: stats(chain(data.old.get(c, {}), data.new_yoy.get(c, {})), years) for c in _CATEGORIES}
    vintage = {
        "source": SOURCE,
        "base2012_last": data.old_last.isoformat(),
        "base2024_last": data.new_last.isoformat() if data.new_last else None,
        "imputed": [d.isoformat() for d in data.imputed],
        "fetched_at": data.fetched_at.isoformat() if data.fetched_at else None,
        "urls": list(data.urls),
        "note": "Base 2012=100 to Dec-2025, extended by the base 2024=100 series' official year-on-year rates; "
        "MoSPI's newest month is provisional.",
    }
    return CpiSummary(st, vintage)


def _r(x: float) -> float:
    return round(x, 2)


def resolve(basis: str, manual_pct: float, cpi: CpiSummary | None, cpi_why: str | None) -> dict[str, Any]:
    """The inflation a goal is simulated with: the mean and spread, a label saying where they come from, and the
    fixed-rate sensitivity rows. A CPI basis whose figure is unknown says so (known False, reason) and runs on the
    goal's own fixed rate, labelled as such; nothing falls back to a default silently."""
    if basis not in BASES:
        raise ValueError(f"unknown inflation basis {basis!r}")
    out: dict[str, Any] = {"basis": basis, "known": True, "reason": None, "value_pct": manual_pct,
                           "mean_pct": manual_pct, "sd_pct": 0.0, "trailing_pct": None, "long_run_pct": None,
                           "premium_pct": None, "as_of": None, "window_start": None, "vintage": None}  # fmt: skip
    fixed_rows = [(f"{manual_pct - SENSITIVITY_PP:g} % (-{SENSITIVITY_PP:g} pp)", manual_pct - SENSITIVITY_PP),
                  (f"{manual_pct:g} % (your rate)", manual_pct),
                  (f"{manual_pct + SENSITIVITY_PP:g} % (+{SENSITIVITY_PP:g} pp)", manual_pct + SENSITIVITY_PP)]  # fmt: skip
    if basis == "manual":
        return out | {"label": f"{manual_pct:g} % a year, your assumption", "sensitivity": fixed_rows}
    cat = basis.removeprefix("cpi_")
    st = cpi.stats.get(cat) if cpi else None
    if st is not None:
        out |= {"trailing_pct": st.trailing_pct, "as_of": st.as_of.isoformat() if st.as_of else None,
                "vintage": cpi.vintage if cpi else None}  # fmt: skip
    if st is None or st.long_run_pct is None or st.sd_pct is None:
        why = cpi_why or (st.why if st else None) or "CPI series not available"
        return out | {"known": False, "reason": why, "value_pct": None,
                      "label": f"CPI {cat} inflation unknown ({why}); simulated at your fixed {manual_pct:g} % a "
                               "year instead",
                      "sensitivity": fixed_rows}  # fmt: skip
    lr = st.long_run_pct
    rows = (
        [(f"CPI {cat}, trailing 12 months to {st.as_of:%b-%Y}", _r(st.trailing_pct))]
        if st.trailing_pct is not None
        else []
    )
    rows += [(f"CPI {cat}, {st.years}-year average", _r(lr)),
             (f"CPI {cat} average +{SENSITIVITY_PP:g} pp", _r(lr + SENSITIVITY_PP))]  # fmt: skip
    gen = cpi.stats.get("general") if cpi else None
    premium = None
    if cat != "general" and gen is not None and gen.long_run_pct is not None:
        premium = lr - gen.long_run_pct
        rows.append((f"CPI general, {gen.years}-year average", _r(gen.long_run_pct)))
    return out | {
        "value_pct": lr, "mean_pct": lr, "sd_pct": st.sd_pct, "long_run_pct": lr, "premium_pct": premium,
        "window_start": st.start.isoformat() if st.start else None,
        "label": f"MoSPI CPI {cat}: {lr:.2f} % a year ({st.years}-year average to {st.as_of:%b-%Y}), drawn each year "
                 f"with a {st.sd_pct:.2f} pp spread",
        "sensitivity": rows,
    }  # fmt: skip

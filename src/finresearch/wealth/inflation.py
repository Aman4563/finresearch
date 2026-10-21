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

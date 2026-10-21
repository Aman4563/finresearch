"""Reference-series maths (#366): pure functions over the stored index closes (refseries.index_eod) and par curves.

* Approximate total-return index from the price index and its dividend yield (NSE's index close file prints the
  yield in percent): TR_t = TR_{t-1} x (P_t / P_{t-1} + y_t / 252). It assumes the yield accrues evenly over 252
  sessions; NSE's own TRI (niftyindices.com) reinvests actual dividends on their ex-dates, so this is labelled
  APPROXIMATE wherever it is shown.
* Par -> zero bootstrap for semi-annual coupon bonds priced at par (FBIL's G-sec par curve is half-yearly
  compounded): P(T_i) = (1 - (c_i / 2) x sum_{j<i} P(T_j)) / (1 + c_i / 2) over the half-year grid, and the
  continuously compounded zero rate r(T) = -ln P(T) / T.
* Short-rate proxy from the 3-month par (or T-bill) yield: r = 2 ln(1 + y / 2).
* Forward with a dividend yield: F = S x e^((r - q) T).
* India VIX as a 30-day variance benchmark against the app's IV30: the correlation of daily changes and the level
  gap's median and median absolute deviation (a stable positive gap is expected: VIX is model-free and includes the
  wings' convexity, ~0.8-1.3 vol points in the 9 days measured in the gap analysis).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import date
from itertools import pairwise
from typing import Any

SESSIONS_PER_YEAR = 252
GRID = 0.5  # semi-annual coupons


def approx_tri(rows: Sequence[tuple[date, float | None, float | None]]) -> list[tuple[date, float | None]]:
    """[(day, close, dividend yield in percent)] oldest first -> [(day, approximate TRI)], based at the first close.
    An unknown close or yield makes that day and every later one None: the chain cannot be continued honestly."""
    out: list[tuple[date, float | None]] = []
    prev_close: float | None = None
    tr: float | None = None
    broken = False
    for i, (d, close, y) in enumerate(rows):
        if broken or close is None or (i > 0 and y is None):
            broken = True
            out.append((d, None))
            continue
        if i == 0:
            tr = close
        else:
            assert tr is not None and prev_close is not None and y is not None
            tr = tr * (close / prev_close + (y / 100) / SESSIONS_PER_YEAR)
        prev_close = close
        out.append((d, tr))
    return out


def zero_curve(par: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """[(tenor years, semi-annual par yield as a fraction)] -> [(tenor, continuous zero rate)] on the half-year
    grid (0.5, 1.0, ...). Tenors off the grid are ignored; a missing grid point below the longest one raises
    ValueError (the bootstrap needs every earlier coupon date)."""
    on_grid = {round(t / GRID): c for t, c in par if abs(t / GRID - round(t / GRID)) < 1e-9 and t > 0}
    if not on_grid:
        raise ValueError("no half-year tenors in the par curve")
    out: list[tuple[float, float]] = []
    discounts = 0.0
    for k in range(1, max(on_grid) + 1):
        if k not in on_grid:
            raise ValueError(f"the par curve has no {k * GRID} year point: cannot bootstrap past it")
        c = on_grid[k]
        p = (1 - c / 2 * discounts) / (1 + c / 2)
        if p <= 0:
            raise ValueError(f"non-positive discount factor at {k * GRID} years")
        discounts += p
        t = k * GRID
        out.append((t, -math.log(p) / t))
    return out


def short_rate_proxy(y_3m: float) -> float:
    """Continuous rate from a semi-annual 3-month yield (fraction)."""
    return 2 * math.log(1 + y_3m / 2)


def forward(spot: float, rate: float, div_yield: float, t_years: float) -> float:
    """F = S e^((r - q) T); rate and yield as continuous fractions."""
    return spot * math.exp((rate - div_yield) * t_years)


def vix_vs_iv30(vix: Sequence[float | None], iv30: Sequence[float | None]) -> dict[str, Any]:
    """Same-day pairs in vol points, oldest first. Pairs with an unknown side are dropped (never read as 0); the
    change correlation uses consecutive kept pairs."""
    pairs = [(v, i) for v, i in zip(vix, iv30, strict=True) if v is not None and i is not None]
    gaps = [v - i for v, i in pairs]
    dv = [b[0] - a[0] for a, b in pairwise(pairs)]
    di = [b[1] - a[1] for a, b in pairwise(pairs)]
    corr = None
    if len(dv) >= 2 and statistics.pstdev(dv) > 0 and statistics.pstdev(di) > 0:
        corr = statistics.correlation(dv, di)
    med = statistics.median(gaps) if gaps else None
    mad = statistics.median([abs(g - med) for g in gaps]) if med is not None else None
    return {"n": len(pairs), "change_correlation": corr, "median_gap": med, "mad_gap": mad}

"""Mutual-fund category rank and percentile: pure arithmetic over month-end NAVs (research item #13, issue #177).

Inputs are each fund's NAV at a grid of month-ends: `grid[0]` is the as-of month-end, `grid[k]` the month-end k
months earlier (each moved back to a weekday). A fund's point at `grid[k]` is its latest NAV on or before that day,
or None when it had none within a few days (the scheme did not exist yet, or was not priced).

Metrics (all funds in a category are measured between the *same* grid dates):

* ``cagr_1y`` / ``cagr_3y`` / ``cagr_5y``: point-to-point annualised return from the NAV at ``grid[12 * y]`` to the
  NAV at ``grid[0]``, ``(end / start) ** (365 / days) - 1`` with the actual NAV dates (fincalc.funds); a fund
  without a NAV at the start is excluded from that metric ("short history").
* ``consistency_3y``: over the last 3 years (``grid[36]..grid[0]``), the share of the 25 rolling 1-year windows
  (``grid[e + 12] -> grid[e]``, e = 0..24, one per month) in which the fund's annualised return was strictly above
  the category's median for the same window. The median is over the funds with a complete 3-year grid (the same
  funds that get this metric). A fund exactly at the median does not count as beating it.
* ``sortino_3y``: from the 36 monthly returns of the last 3 years. Minimum acceptable return (MAR) is a stated
  annual rate (default 6.5 %, the app's risk-free default) taken as MAR / 12 a month. Downside deviation is
  ``sqrt(sum(min(0, r - MAR/12)^2) / N)`` over *all* N months (the full-sample convention of Sortino & Price 1994),
  annualised by sqrt(12); the numerator is ``(mean(r) - MAR/12) x 12`` (fincalc.funds.sortino_ratio with 12 periods a
  year). A fund with no month below the MAR has an infinite ratio: it ranks first and its value is shown as None
  with ``no_downside``.
* ``max_drawdown_3y``: the worst fall from a month-end peak to a later month-end low over the 37 month-ends of the
  last 3 years, ``trough / peak - 1`` (negative; 0 if it never fell). Month-end sampling misses falls that recovered
  within a month, so it understates daily drawdowns.
* ``ter``: the direct plan's total expense ratio, % a year, from AMFI's TER file (lower is better).

Rank is competition ranking among the funds that have the metric: 1 + the number of funds strictly better, so
ties share a rank (1, 2, 2, 4). Percentile is the share of those funds this one beats, ties counting half
(fincalc.funds.percentile_rank): the best of 34 is 98.5 %, the worst 1.5 %. A metric is ranked only when at least
`min_ranked` funds have it.

No composite score. The per-metric ranks are shown side by side and the viewer chooses the column to sort by:
any weighting of these metrics would be an arbitrary judgment presented as precision, the return metrics are
strongly correlated with each other (a weighted sum mostly double-counts recent return), and past-return ranks
persist only weakly (Carhart 1997, https://doi.org/10.1111/j.1540-6261.1997.tb03808.x; SPIVA India). Cost (TER) is
the one input with reliable persistence, so it is shown as its own column rather than diluted into a blend.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise

from finresearch.fincalc import funds as F
from finresearch.fincalc.market import max_drawdown

Point = tuple[date, Decimal] | None

MONTHS_3Y = 36
ROLLING_WINDOWS_3Y = MONTHS_3Y - 12 + 1  # 25 one-year windows ending at each of the last 25 month-ends
MIN_RANKED = 5  # fewer funds with a metric than this: no rank for anyone (a rank of 2 of 3 says little)
MAR_DEFAULT = Decimal("0.065")  # the app's risk-free default (api.markets.RF_DEFAULT); stated in every output


@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    higher_is_better: bool
    unit: str  # "fraction" (0.12 = 12 %), "ratio", "pct" (TER in % a year)


METRICS: tuple[MetricDef, ...] = (
    MetricDef("cagr_1y", "1-year return", True, "fraction"),
    MetricDef("cagr_3y", "3-year CAGR", True, "fraction"),
    MetricDef("cagr_5y", "5-year CAGR", True, "fraction"),
    # 1-year windows, so it differs from the Consistency card's default 3-year windows (#200: 60 % here vs 16/16 there)
    MetricDef(
        "consistency_3y", "Consistency: 1-year windows above the median (last 3 years)", True, "fraction"
    ),
    MetricDef("sortino_3y", "Sortino ratio (3 years, monthly)", True, "ratio"),
    MetricDef("max_drawdown_3y", "Max drawdown (3 years, month-end)", True, "fraction"),
    MetricDef("ter", "Expense ratio (direct plan)", False, "pct"),
)
METRIC_KEYS = tuple(m.key for m in METRICS)
_BY_KEY = {m.key: m for m in METRICS}


def month_end(year: int, month: int) -> date:
    """The last day of a month, moved back to a weekday (the same anchors as signals.fund.quarter_ends, so the
    quarter-end NAV snapshots are shared)."""
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def latest_month_end(on_or_before: date) -> date:
    """The latest (weekday-adjusted) month-end on or before a day."""
    d = month_end(on_or_before.year, on_or_before.month)
    if d <= on_or_before:
        return d
    y, m = (
        (on_or_before.year, on_or_before.month - 1) if on_or_before.month > 1 else (on_or_before.year - 1, 12)
    )
    return month_end(y, m)


def month_grid(as_of: date, months: int) -> list[date]:
    """[as_of, the month-end before, ...], `months + 1` dates; `as_of` must be a (weekday-adjusted) month-end."""
    out, y, m = [], as_of.year, as_of.month
    for _ in range(months + 1):
        out.append(month_end(y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


@dataclass(frozen=True)
class RankInput:
    code: str
    name: str
    points: Sequence[Point]  # aligned with the grid: points[k] is the NAV at grid[k]
    ter: float | None = None
    amc: str | None = None


@dataclass
class FundMetrics:
    code: str
    values: dict[str, float | None] = field(default_factory=dict)
    no_downside: bool = False  # Sortino infinite: no month below the MAR
    reasons: dict[str, str] = field(default_factory=dict)  # metric -> why it is missing

    def sort_value(self, key: str) -> float | None:
        if key == "sortino_3y" and self.no_downside:
            return math.inf
        return self.values.get(key)


def cagr(points: Sequence[Point], years: int) -> float | None:
    """Annualised return from points[12 * years] to points[0]; None if either is missing."""
    k = 12 * years
    if len(points) <= k or points[0] is None or points[k] is None:
        return None
    (d1, v1), (d0, v0) = points[0], points[k]
    if d1 <= d0:
        return None
    return float(F.annualised_return(v0, v1, d0, d1))


def three_year_path(points: Sequence[Point]) -> list[tuple[date, Decimal]] | None:
    """The 37 month-end NAVs of the last 3 years in date order, or None if any is missing."""
    if len(points) <= MONTHS_3Y:
        return None
    pts = list(points[: MONTHS_3Y + 1])
    if any(p is None for p in pts):
        return None
    path = list(reversed(pts))  # oldest first
    if any(b[0] <= a[0] for a, b in pairwise(path)):
        return None  # two grid dates fell back to the same NAV: the fund was not priced in between
    return path  # type: ignore[return-value]


def rolling_1y(path: Sequence[tuple[date, Decimal]]) -> list[float]:
    """Annualised 1-year returns ending at each month-end from path[12] to path[36] (25 windows, oldest first)."""
    return [float(F.annualised_return(path[i - 12][1], path[i][1], path[i - 12][0], path[i][0]))
            for i in range(12, len(path))]  # fmt: skip


def sortino(path: Sequence[tuple[date, Decimal]], mar_annual: Decimal) -> tuple[float | None, bool]:
    """(annualised Sortino of the monthly returns, no_downside). See the module docstring."""
    try:
        return float(F.sortino_ratio(path, mar_annual, periods_per_year=12)), False
    except (
        ValueError
    ):  # no month below the MAR: downside deviation is zero, the ratio is unbounded. (The only
        # other ValueErrors, too few or non-positive NAVs, cannot occur: three_year_path gives 37 dated NAVs > 0.)
        return None, True


def drawdown(path: Sequence[tuple[date, Decimal]]) -> float:
    return float(max_drawdown([v for _, v in path]).max_drawdown)


def fund_metrics(f: RankInput, mar_annual: Decimal) -> tuple[FundMetrics, list[float] | None]:
    """Every metric that needs only this fund's NAVs, and its rolling 1-year returns (for consistency)."""
    m = FundMetrics(f.code)
    for y in (1, 3, 5):
        v = cagr(f.points, y)
        m.values[f"cagr_{y}y"] = v
        if v is None:
            m.reasons[f"cagr_{y}y"] = (
                f"no NAV {y} year{'s' if y > 1 else ''} before the as-of date (short history)"
            )
    path = three_year_path(f.points)
    rolls = None
    if path is None:
        for k in ("consistency_3y", "sortino_3y", "max_drawdown_3y"):
            m.values[k] = None
            m.reasons[k] = "no complete 3-year month-end history (short history or unpriced months)"
    else:
        rolls = rolling_1y(path)
        m.values["sortino_3y"], m.no_downside = sortino(path, mar_annual)
        m.values["max_drawdown_3y"] = drawdown(path)
    m.values["ter"] = f.ter
    if f.ter is None:
        m.reasons["ter"] = "no direct-plan TER matched in AMFI's TER file"
    return m, rolls


def consistency(rolls: dict[str, list[float]], min_funds: int = MIN_RANKED) -> dict[str, float]:
    """Per fund, the share of windows in which its return was strictly above the median of all funds' returns in
    the same window. Empty when fewer than `min_funds` funds have the windows."""
    if len(rolls) < min_funds:
        return {}
    n = min(len(r) for r in rolls.values())
    medians = [F.median([r[i] for r in rolls.values()]) for i in range(n)]
    return {c: sum(1 for i in range(n) if r[i] > medians[i]) / n for c, r in rolls.items()}


def rank(values: dict[str, float], higher_is_better: bool = True) -> dict[str, tuple[int, float]]:
    """code -> (competition rank, percentile 0..1). Ties share the better rank; the percentile counts ties half."""
    signed = {c: (v if higher_is_better else -v) for c, v in values.items()}
    vals = list(signed.values())
    out = {}
    for c, v in signed.items():
        out[c] = (
            sum(1 for x in vals if x > v) + 1,
            F.percentile_rank(vals, v),
        )  # inf (no downside) compares fine
    return out


def rank_category(funds: Sequence[RankInput], mar_annual: Decimal = MAR_DEFAULT,
                  min_ranked: int = MIN_RANKED) -> dict:  # fmt: skip
    """Every fund's metrics, per-metric rank and percentile within one category (see the module docstring).

    Returns {"size", "counts": {metric: funds with it}, "ranked": {metric: bool}, "funds": [{code, name, amc,
    values, ranks, percentiles, no_downside, missing: {metric: reason}}]}."""
    metrics: dict[str, FundMetrics] = {}
    rolls: dict[str, list[float]] = {}
    for f in funds:
        m, r = fund_metrics(f, mar_annual)
        metrics[f.code] = m
        if r is not None:
            rolls[f.code] = r
    cons = consistency(rolls, min_ranked)
    for code, m in metrics.items():
        if code in rolls:
            m.values["consistency_3y"] = cons.get(code)
            if code not in cons:
                m.reasons["consistency_3y"] = f"fewer than {min_ranked} funds with 3 years of history"
    ranks: dict[str, dict[str, tuple[int, float]]] = {}
    counts: dict[str, int] = {}
    ranked: dict[str, bool] = {}
    for md in METRICS:
        have = {c: v for c, m in metrics.items() if (v := m.sort_value(md.key)) is not None}
        counts[md.key] = len(have)
        ranked[md.key] = len(have) >= min_ranked
        ranks[md.key] = rank(have, md.higher_is_better) if ranked[md.key] else {}
    rows = []
    for f in funds:
        m = metrics[f.code]
        rows.append({"code": f.code, "name": f.name, "amc": f.amc,
                     "values": {k: _round(m.values.get(k), k) for k in METRIC_KEYS},
                     "ranks": {k: ranks[k][f.code][0] if f.code in ranks[k] else None for k in METRIC_KEYS},
                     "percentiles": {k: round(ranks[k][f.code][1], 4) if f.code in ranks[k] else None
                                     for k in METRIC_KEYS},
                     "no_downside": m.no_downside, "missing": m.reasons})  # fmt: skip
    return {"size": len(funds), "counts": counts, "ranked": ranked, "funds": rows}


def _round(v: float | None, key: str) -> float | None:
    if v is None:
        return None
    return round(v, 4) if _BY_KEY[key].unit in ("ratio", "pct") else round(v, 6)

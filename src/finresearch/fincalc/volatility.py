"""Implied-volatility context for options analysis (docs/dev/RESEARCH_ROADMAP.md §D.5, roadmap item 18).

- ATM IV: the mean of the call and put implied volatility at the strike nearest the underlying (NSE publishes IV per
  strike, in %).
- IV rank  IVR = (IV − min) / (max − min) over the last 252 recorded days.
- IV percentile  IVP = share of the last 252 recorded days with IV below today's.
  Both need a stored daily series; below MIN_DAYS observations they are not computed and the caller says
  "insufficient history (n days)". About a year of history is needed before IVR is meaningful (roadmap item 18).
- Skew: 25-delta put IV − 25-delta call IV, in volatility points, from one chain. Each strike's delta comes from
  Black–Scholes with that strike's own IV; the IV at |Δ| = 0.25 is interpolated linearly in delta.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from finresearch.fincalc.options import greeks

MIN_DAYS = 60
WINDOW = 252


@dataclass(frozen=True)
class StrikeIv:
    strike: float
    call_iv: float | None  # annual %, as NSE publishes it
    put_iv: float | None


def atm_iv(rows: Sequence[StrikeIv], spot: float) -> tuple[float, float] | None:
    """(ATM strike, ATM IV %): the mean of call and put IV at the strike nearest `spot` (either alone if only one is
    published). None when the nearest strike has no IV."""
    if not rows:
        return None
    row = min(rows, key=lambda r: abs(r.strike - spot))
    ivs = [x for x in (row.call_iv, row.put_iv) if x]
    return (row.strike, sum(ivs) / len(ivs)) if ivs else None


def _iv_at_delta(points: list[tuple[float, float]], target: float) -> float | None:
    """Linear interpolation of IV at `target` delta from (delta, iv) points; None if the target is not bracketed."""
    pts = sorted(points)
    for (d0, v0), (d1, v1) in itertools.pairwise(pts):
        if d0 <= target <= d1:
            return v0 if d1 == d0 else v0 + (v1 - v0) * (target - d0) / (d1 - d0)
    return None


def skew_25d(rows: Sequence[StrikeIv], spot: float, t_years: float, rate: float) -> float | None:
    """25-delta put IV − 25-delta call IV (volatility points). None when the chain does not span both deltas."""
    if t_years <= 0 or spot <= 0:
        return None
    calls, puts = [], []
    for r in rows:
        if r.call_iv and r.call_iv > 0:
            calls.append((greeks("call", spot, r.strike, t_years, rate, r.call_iv / 100).delta, r.call_iv))
        if r.put_iv and r.put_iv > 0:
            puts.append((greeks("put", spot, r.strike, t_years, rate, r.put_iv / 100).delta, r.put_iv))
    c, p = _iv_at_delta(calls, 0.25), _iv_at_delta(puts, -0.25)
    return None if c is None or p is None else p - c


@dataclass(frozen=True)
class IvStats:
    n: int  # observations in the window
    current: float | None
    rank: float | None  # 0..1
    percentile: float | None  # 0..1
    low: float | None
    high: float | None
    status: str  # "ok" or "insufficient history (n days)"


def iv_stats(series: Sequence[float], window: int = WINDOW, min_days: int = MIN_DAYS) -> IvStats:
    """IV rank and percentile of the last value of `series` (oldest first) against the last `window` values."""
    s = [float(x) for x in series if x is not None][-window:]
    n = len(s)
    cur = s[-1] if s else None
    if n < min_days:
        return IvStats(n, cur, None, None, min(s) if s else None, max(s) if s else None,
                       f"insufficient history ({n} day{'s' if n != 1 else ''}; needs {min_days})")  # fmt: skip
    lo, hi = min(s), max(s)
    rank = (cur - lo) / (hi - lo) if hi > lo else 0.5
    pct = sum(1 for x in s[:-1] if x < cur) / (n - 1)
    return IvStats(n, cur, rank, pct, lo, hi, "ok")

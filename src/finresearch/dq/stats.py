"""Pure statistics for the data-quality checks (#368). No database.

* robust z = (x - median_N) / (1.4826 x MAD_N) over the last N loaded sessions (the issue's completeness test, flagged
  at |z| > 6). 1.4826 scales the MAD to a normal sigma (Rousseeuw & Croux 1993). Too short a history, or a MAD of 0 with
  x off the median, gives None: unknown, never "ok".
* freshness: the last session whose file should already be out (its SLA time has passed, IST) and how many sessions
  the last loaded day lags it.
"""

from __future__ import annotations

import statistics
from collections.abc import Collection, Sequence
from datetime import date, datetime, time, timedelta

from finresearch.fincalc.dates import business_days_between, is_business_day, to_ist

MAD_SCALE = 1.4826
Z_LIMIT = 6.0
MIN_HISTORY = 20


def robust_z(x: float, history: Sequence[float], min_n: int = MIN_HISTORY) -> float | None:
    if len(history) < min_n:
        return None
    med = statistics.median(history)
    mad = statistics.median([abs(h - med) for h in history])
    if mad == 0:
        return 0.0 if x == med else None
    return (x - med) / (MAD_SCALE * mad)


def z_status(z: float | None, limit: float = Z_LIMIT) -> str:
    if z is None:
        return "unknown"
    return "fail" if abs(z) > limit else "ok"


def mad_status(x: float, history: Sequence[float], k: float = 4.0, min_n: int = MIN_HISTORY) -> str:
    """|x - median| > k x MAD (raw MAD, the issue's VIX - IV30 rule) -> fail; short history -> unknown."""
    if len(history) < min_n:
        return "unknown"
    med = statistics.median(history)
    mad = statistics.median([abs(h - med) for h in history])
    if mad == 0:
        return "ok" if x == med else "fail"
    return "fail" if abs(x - med) > k * mad else "ok"


def expected_day(now: datetime, sla: time, holidays: Collection[date]) -> date:
    """The last trading day whose file should be published by `now` (IST): today once the SLA has passed."""
    ist = to_ist(now)
    d = ist.date() if ist.time() >= sla else ist.date() - timedelta(days=1)
    while not is_business_day(d, holidays):
        d -= timedelta(days=1)
    return d


def lag_sessions(last: date | None, expected: date, holidays: Collection[date]) -> int | None:
    """Trading sessions in (last, expected]: 0 when up to date (or ahead, a special session), None when never loaded."""
    if last is None:
        return None
    return max(0, business_days_between(last, expected, holidays))

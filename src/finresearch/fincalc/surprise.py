"""Earnings surprise without consensus data, and the results-day price reaction (issue #181; pre-registration in
evals/experiments/earnings_surprise/PREREG.md). Pure functions; evals.earnings_surprise and the API feed them.

- SUE (standardised unexpected earnings), seasonal random walk: d_q = X_q - X_{q-4}; SUE_q = d_q / s, s = the sample
  standard deviation (n - 1) of d over the 8 quarters before q, at least 6 of them (Bernard & Thomas 1990, J.
  Accounting & Economics 13(4) 305-340, use a standardised seasonal forecast error; Foster, Olsen & Shevlin 1984,
  The Accounting Review 59(4), compare seasonal random-walk expectation models. Neither paper's exact model was
  re-read: this is the operational definition registered in PREREG.md, with no drift term).
- Point in time: only quarters whose results were broadcast at or before quarter q's own broadcast are used; EPS is as
  first reported, put on q's share basis by dividing by every split/bonus factor that went ex after the earlier
  filing and on or before q's (fincalc.signals.action_factor).
- t0: the session of the broadcast when it came before the 15:30 IST close (NSE equity market hours 09:15-15:30),
  else the next session; a timestamp without a time of day counts as after the close.
- Abnormal return over [a, b] = stock return minus index return from the close of t0 + a - 1 to the close of t0 + b
  (market-adjusted: no beta). [0,+1] is the reaction, [+2,+60] the drift.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from statistics import stdev
from zoneinfo import ZoneInfo

from finresearch.fincalc.signals import action_factor

IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = time(15, 30)  # NSE normal market close, IST
WINDOW = 8  # quarters of past seasonal differences behind s
MIN_DIFFS = 6


@dataclass(frozen=True)
class Quarter:
    end: date  # quarter end
    announced: datetime  # first broadcast of the results (IST)
    value: float | None  # EPS (rupees per share) or revenue (rupees), as first reported
    filed: datetime | None = (
        None  # the broadcast of the filing the value comes from (defaults to `announced`)
    )


def quarter_back(end: date, k: int) -> date:
    """The quarter end k quarters before `end` (a calendar quarter end: 31-Mar, 30-Jun, 30-Sep, 31-Dec)."""
    m = end.year * 12 + end.month - 1 - 3 * k
    y, mo = divmod(m, 12)
    nxt = date(y + (mo + 1) // 12, (mo + 1) % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def split_factor(actions: Sequence[tuple[date, str]], after: date, upto: date) -> float:
    """Product of split/bonus factors with an ex-date in (after, upto]."""
    f = 1.0
    for ex, subject in actions:
        if after < ex <= upto and (x := action_factor(subject)):
            f *= x
    return f


def sue(quarters: Sequence[Quarter], end: date, actions: Sequence[tuple[date, str]] = (), *,
        per_share: bool = True) -> tuple[float | None, str | None]:  # fmt: skip
    """SUE of the quarter ending `end`, or (None, reason). `per_share` applies the split/bonus adjustment (EPS)."""
    by_end = {q.end: q for q in quarters}
    cur = by_end.get(end)
    if cur is None or cur.value is None:
        return None, "no figure for the quarter"
    asof = cur.announced
    known = {e: q for e, q in by_end.items() if q.announced <= asof and q.value is not None}

    def val(e: date) -> float | None:
        q = known.get(e)
        if q is None:
            return None
        if not per_share:
            return q.value
        filed = (q.filed or q.announced).astimezone(IST).date()
        return q.value / split_factor(actions, filed, (cur.filed or asof).astimezone(IST).date())

    def diff(e: date) -> float | None:
        a, b = val(e), val(quarter_back(e, 4))
        return None if a is None or b is None else a - b

    d = diff(end)
    if d is None:
        return None, "no figure for the same quarter a year earlier"
    hist = [x for k in range(1, WINDOW + 1) if (x := diff(quarter_back(end, k))) is not None]
    if len(hist) < MIN_DIFFS:
        return None, f"only {len(hist)} of {WINDOW} past seasonal differences (need {MIN_DIFFS})"
    s = stdev(hist)
    if s == 0:
        return None, "past seasonal differences do not vary"
    return d / s, None


# SEBI LODR Reg. 33(3)(a): quarterly results within 45 days of the quarter end; 33(3)(d): annual audited results
# within 60 days. COVID extensions [U: circulars not re-read]: Mar-2020 quarter by 31-Jul-2020, Jun-2020 by
# 15-Sep-2020, Mar-2021 by 30-Jun-2021. See evals/experiments/earnings_surprise/ADDENDUM.md.
DEADLINE_EXTENSIONS = {date(2020, 3, 31): date(2020, 7, 31), date(2020, 6, 30): date(2020, 9, 15),
                       date(2021, 3, 31): date(2021, 6, 30)}  # fmt: skip
DEADLINE_SLACK_DAYS = 7


def results_deadline(end: date) -> date:
    """The last day results for the quarter ending `end` may legally be published."""
    if end in DEADLINE_EXTENSIONS:
        return DEADLINE_EXTENSIONS[end]
    return date.fromordinal(end.toordinal() + (60 if end.month == 3 else 45))


def plausible_announcement(end: date, announced: datetime) -> bool:
    """A broadcast after the deadline (+ slack) is taken as a later re-upload, not the announcement (ADDENDUM 1)."""
    day = announced.astimezone(IST).date()
    return end < day <= date.fromordinal(results_deadline(end).toordinal() + DEADLINE_SLACK_DAYS)


def event_session(announced: datetime, has_time: bool, sessions: Sequence[date]) -> date | None:
    """t0: the broadcast's own session when it came before the 15:30 IST close, else the next session. `sessions` is
    the sorted trading calendar; None when it does not reach t0."""
    local = announced.astimezone(IST)
    day = local.date()
    same_day_ok = has_time and local.time() < MARKET_CLOSE
    i = bisect.bisect_left(sessions, day) if same_day_ok else bisect.bisect_right(sessions, day)
    return sessions[i] if i < len(sessions) else None


def abnormal_return(stock: Mapping[date, float], index: Mapping[date, float], sessions: Sequence[date], t0: date,
                    a: int, b: int) -> float | None:  # fmt: skip
    """Stock return minus index return from the close of t0+a-1 to the close of t0+b (trading days on `sessions`);
    None when a close is missing on either date or the window runs past the calendar."""
    i = bisect.bisect_left(sessions, t0)
    if i >= len(sessions) or sessions[i] != t0:
        return None
    lo, hi = i + a - 1, i + b
    if lo < 0 or hi >= len(sessions):
        return None
    d0, d1 = sessions[lo], sessions[hi]
    if not all(d in stock and d in index for d in (d0, d1)):
        return None
    return stock[d1] / stock[d0] - index[d1] / index[d0]


def deciles(values: Sequence[float]) -> list[int]:
    """Decile 1..10 of each value by rank within the list (ties broken by position): rank r of n -> 1 + 10r // n."""
    order = sorted(range(len(values)), key=lambda k: values[k])
    out = [0] * len(values)
    for r, k in enumerate(order):
        out[k] = 1 + 10 * r // len(values)
    return out


def decile_of(x: float, reference: Sequence[float]) -> int | None:
    """Decile 1..10 of x against a reference distribution: 1 + 10 * (share of the reference below x), capped at 10."""
    if not reference:
        return None
    below = sum(1 for v in reference if v < x)
    return min(10, 1 + 10 * below // len(reference))


@dataclass(frozen=True)
class Estimate:
    value: float
    se: float | None
    t: float | None
    n: int
    clusters: int


def _clustered(psi: Sequence[float], clusters: Sequence[object]) -> tuple[float | None, int]:
    """Cluster-robust standard error from per-observation influence values: sqrt(G/(G-1) * sum_g (sum_{i in g}
    psi_i)^2) (the CR1-style small-sample factor; Cameron & Miller 2015, J. Human Resources 50(2))."""
    sums: dict[object, float] = {}
    for p, c in zip(psi, clusters, strict=True):
        sums[c] = sums.get(c, 0.0) + p
    g = len(sums)
    if g < 2:
        return None, g
    return math.sqrt(g / (g - 1) * sum(s * s for s in sums.values())), g


def _est(value: float, psi: Sequence[float], clusters: Sequence[object]) -> Estimate:
    se, g = _clustered(psi, clusters)
    return Estimate(value, se, value / se if se else None, len(psi), g)


def clustered_mean(x: Sequence[float], clusters: Sequence[object]) -> Estimate:
    """Mean of x with a standard error clustered on `clusters` (influence of each observation: (x_i - mean) / n)."""
    n = len(x)
    m = sum(x) / n
    return _est(m, [(v - m) / n for v in x], clusters)


def clustered_diff(
    a: Sequence[float], ca: Sequence[object], b: Sequence[float], cb: Sequence[object]
) -> Estimate:
    """mean(a) - mean(b), clustered: the OLS slope on a group dummy with a cluster-robust error (G/(G-1) factor
    only; Stata/statsmodels also multiply by (N-1)/(N-K), under 0.3 % larger at N ~ 200)."""
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    psi = [(v - ma) / len(a) for v in a] + [-(v - mb) / len(b) for v in b]
    return _est(ma - mb, psi, list(ca) + list(cb))

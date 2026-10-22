"""Event volatility (P3.8, #391): the implied move of a scheduled event from the option term structure, and the
history of realised event moves. Pure, no I/O.

Total implied variance to expiry j is w_j = sigma_j^2 x tau_j, with tau on a clock that holds no event variance (the
business clock with events EXCLUDED, or the calendar clock; the caller states which). The model is

    w(tau_j) = sigma_d^2 tau_j + sum_e J_e^2 x 1{T_j >= e}

* Two expiries both after the event: J^2 = (w1 tau2 - w2 tau1) / (tau2 - tau1) = (sigma1^2 - sigma2^2) / (1/tau1 -
  1/tau2), base variance sigma_d^2 = (w2 - w1) / (tau2 - tau1).
* One expiry before the event and one after (T1 < e <= T2): J^2 = tau2 x (sigma2^2 - sigma1^2).
* Three or more expiries: least squares with every J_e^2 >= 0 (`fit`).

J^2 is reported only when positive. When the term structure does not show the event (sigma1 <= sigma2 for two
expiries after it, a missing expiry, events between the same two expiries) the move is unknown with the reason,
never a zero move. The one-sigma move is J; the expected |move| of a normal move is sqrt(2/pi) J (about 0.80 J) and
its median 0.6745 J. Display only: nothing here feeds the card's probabilities (any forecast use needs its own
registration).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import NormalDist, median

from finresearch.eventcal.calendar import NSE_CLOSE, Event, india_day, known_at
from finresearch.fincalc.dates import to_ist

MEAN_ABS = math.sqrt(2 / math.pi)  # E|Z| for a standard normal Z
MEDIAN_ABS = NormalDist().inv_cdf(0.75)  # median |Z| = 0.6745


@dataclass(frozen=True)
class Point:
    """One expiry's at-the-money implied volatility (annual, a fraction) and its time to expiry in years."""

    expiry: date
    tau: float | None
    sigma: float | None


@dataclass(frozen=True)
class Implied:
    j2: float | None  # the event's variance (a fraction squared, not annualised); None = unknown (see reason)
    base_var: float | None = None  # sigma_d^2, annual
    method: str = ""
    reason: str | None = None

    @property
    def move(self) -> float | None:
        """The implied one-sigma event move (a log-return fraction)."""
        return None if self.j2 is None else math.sqrt(self.j2)

    @property
    def expected_abs(self) -> float | None:
        return None if self.j2 is None else MEAN_ABS * math.sqrt(self.j2)

    @property
    def median_abs(self) -> float | None:
        return None if self.j2 is None else MEDIAN_ABS * math.sqrt(self.j2)


def _usable(*ps: Point) -> str | None:
    for p in ps:
        if p.sigma is None or p.tau is None or not math.isfinite(p.sigma) or not math.isfinite(p.tau):
            return f"no implied volatility for the {p.expiry} expiry"
        if p.sigma <= 0 or p.tau <= 0:
            return f"the {p.expiry} expiry has no time or volatility left"
    if len({p.expiry for p in ps}) < len(ps) or len({p.tau for p in ps}) < len(ps):
        return "the expiries are not distinct: no term structure"
    return None


def two_after(near: Point, far: Point) -> Implied:
    """Both expiries after the event (near before far)."""
    if bad := _usable(near, far):
        return Implied(None, method="two_after", reason=bad)
    w1, w2 = near.sigma**2 * near.tau, far.sigma**2 * far.tau  # type: ignore[operator]
    base = (w2 - w1) / (far.tau - near.tau)  # type: ignore[operator]
    gap = near.sigma**2 - far.sigma**2  # type: ignore[operator]  # the sign test is exact on the IVs themselves
    j2 = gap / (1 / near.tau - 1 / far.tau)  # type: ignore[operator]
    if gap <= 0 or base <= 0:
        return Implied(None, method="two_after",
                       reason="the event cannot be identified from these expiries: the near IV is not above the "
                              "far IV by enough to imply an event premium")  # fmt: skip
    return Implied(j2, base, "two_after")


def straddling(before: Point, after: Point) -> Implied:
    """One expiry before the event and one after it: the earlier expiry's variance is all base variance."""
    if bad := _usable(before, after):
        return Implied(None, method="straddling", reason=bad)
    base = before.sigma**2  # type: ignore[operator]
    gap = after.sigma**2 - base  # type: ignore[operator]
    j2 = after.tau * gap  # type: ignore[operator]
    if gap <= 0:
        return Implied(None, base, "straddling",
                       "the event cannot be identified: the expiry after it is not priced above the one before")  # fmt: skip
    return Implied(j2, base, "straddling")


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Least squares by the normal equations and Gaussian elimination (a few unknowns); None when singular."""
    k = len(a[0])
    m = [[sum(r[i] * r[j] for r in a) for j in range(k)] + [sum(r[i] * y for r, y in zip(a, b, strict=True))]
         for i in range(k)]  # fmt: skip
    for c in range(k):
        p = max(range(c, k), key=lambda i: abs(m[i][c]))
        if abs(m[p][c]) < 1e-18 * max(1.0, max(abs(x) for row in m for x in row[:k])):
            return None
        m[c], m[p] = m[p], m[c]
        for i in range(k):
            if i != c:
                f = m[i][c] / m[c][c]
                m[i] = [x - f * y for x, y in zip(m[i], m[c], strict=True)]
    return [m[i][k] / m[i][i] for i in range(k)]


def fit(points: list[Point], events: list[date]) -> dict[date, Implied]:
    """Every event's J^2 from all the expiries at once: least squares of w_j on (tau_j, 1{T_j >= e}) with every
    J_e^2 >= 0 (the exact non-negative solution: the best feasible fit over which events are held at zero, at most
    2^events small solves). An event no expiry follows, or sharing its expiries with another event, is unknown; so is
    every event when there are fewer expiries than unknowns or the base variance is not positive."""
    out: dict[date, Implied] = {}
    pts = sorted(points, key=lambda p: p.expiry)
    if bad := _usable(*pts) if pts else "no expiries":
        return {e: Implied(None, method="fit", reason=bad) for e in events}
    cols: dict[tuple[int, ...], list[date]] = {}
    for e in sorted(set(events)):
        col = tuple(int(p.expiry >= e) for p in pts)
        if not any(col):
            out[e] = Implied(None, method="fit", reason="no expiry after the event: its move is not priced yet")
        else:
            cols.setdefault(col, []).append(e)
    for same in cols.values():
        if len(same) > 1:
            for e in same:
                out[e] = Implied(None, method="fit", reason="events between the same expiries cannot be told apart: "
                                 + ", ".join(str(x) for x in same if x != e))  # fmt: skip
    solo = [(col, es[0]) for col, es in cols.items() if len(es) == 1]
    if not solo:
        return out
    if 1 + len(solo) > len(pts):
        out.update({e: Implied(None, method="fit", reason=f"too few expiries ({len(pts)}) to separate the base "
                               f"variance and {len(solo)} event(s)") for _, e in solo})  # fmt: skip
        return out
    w = [p.sigma**2 * p.tau for p in pts]  # type: ignore[operator]
    best: tuple[float, list[float], list[int]] | None = None
    for mask in range(1 << len(solo)):
        free = [i for i in range(len(solo)) if mask >> i & 1]
        a = [[p.tau] + [solo[i][0][j] for i in free] for j, p in enumerate(pts)]  # type: ignore[list-item]
        x = _solve(a, w)  # type: ignore[arg-type]
        if x is None or x[0] <= 0 or any(v <= 0 for v in x[1:]):
            continue
        ssr = sum((sum(c * v for c, v in zip(r, x, strict=True)) - y) ** 2 for r, y in zip(a, w, strict=True))
        if best is None or ssr < best[0] - 1e-30:
            best = (ssr, x, free)
    if best is None:
        out.update({e: Implied(None, method="fit", reason="no positive base variance fits these expiries")
                    for _, e in solo})  # fmt: skip
        return out
    _, x, free = best
    for i, (_, e) in enumerate(solo):
        out[e] = (Implied(x[1 + free.index(i)], x[0], "fit") if i in free else
                  Implied(None, x[0], "fit", "the term structure shows no premium for this event"))  # fmt: skip
    return out


# ---------------------------------------------------------------- realised event moves (point in time)
MAX_GAP_DAYS = 5  # a close more than 5 calendar days from the event's session is a gap in the data, not a holiday


@dataclass(frozen=True)
class Move:
    """One event's realised log return from the last close before its India session to the first close on or after
    it; r None = unknown (reason)."""

    day: date  # the event's India session (calendar.india_day)
    r: float | None
    start: date | None
    end: date | None
    reason: str | None
    title: str = ""
    j2: float | None = None  # the implied J^2 the term structure showed before it, when the caller pairs one


@dataclass(frozen=True)
class TypeStats:
    type: str
    moves: list[Move] = field(default_factory=list)

    @property
    def known(self) -> list[float]:
        return [m.r for m in self.moves if m.r is not None]

    @property
    def n(self) -> int:
        return len(self.known)

    @property
    def median_abs(self) -> float | None:
        return median(abs(r) for r in self.known) if self.known else None

    @property
    def mean_r2(self) -> float | None:
        return sum(r * r for r in self.known) / self.n if self.known else None


def realised_move(e: Event, closes: Mapping[date, float]) -> Move:
    d = india_day(e)
    before = [x for x in closes if x < d and closes[x] is not None]
    after = [x for x in closes if x >= d and closes[x] is not None]
    a, b = (max(before) if before else None), (min(after) if after else None)
    if a is None or b is None or (d - a).days > MAX_GAP_DAYS or (b - d).days > MAX_GAP_DAYS:
        return Move(d, None, a, b, "no stored close on both sides of the event", e.title)
    ca, cb = closes[a], closes[b]
    if ca <= 0 or cb <= 0:
        return Move(d, None, a, b, "a non-positive close", e.title)
    return Move(d, math.log(cb / ca), a, b, None, e.title)


def close_known(day: date, as_of: datetime) -> bool:
    """A session's close is known after 15:30 IST that day."""
    t = to_ist(as_of)
    return day < t.date() or (day == t.date() and t.time() >= NSE_CLOSE)


def history(events: list[Event], closes: Mapping[date, float], as_of: datetime) -> dict[str, TypeStats]:
    """Realised moves by event type as known at `as_of`: only events public by then (and not moved away), whose India
    session has closed, with only the closes known by then."""
    seen = {d: c for d, c in closes.items() if close_known(d, as_of)}
    out: dict[str, TypeStats] = {}
    for e in sorted(known_at(events, as_of), key=lambda x: (india_day(x), x.type)):
        if not close_known(india_day(e), as_of):
            continue
        out.setdefault(e.type, TypeStats(e.type)).moves.append(realised_move(e, seen))
    return out

"""Book-level exposures and a full-revaluation scenario engine for the F&O book (#394). Analysis only: nothing here
places, modifies or cancels an order. Pure arithmetic; the caller gathers legs, marks and forwards.

Every option leg is repriced with the app's own Black-76 IV, inverted from the leg's mark (its mid, or its LTP,
flagged, when there is no quote) on the forward of its own underlying and expiry: the same-expiry future, or the
put-call parity forward when there is none. Vendor IVs and fnobook's Black-Scholes greeks are not used. Black-76 is
`options.bs_price` with the forward as the spot and the dividend yield equal to the rate (the drift cancels:
e^{-rT}[F N(d1) - K N(d2)]), so the pricing code is the app's one implementation. T is to the minute, to 15:30 IST on
the expiry day (#359). A leg whose mark, forward or IV is unknown, or that has expired, is unpriced with a reason: the
book's exposures and scenarios are then incomplete and say so, never counted as 0.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from finresearch.fincalc import options as o
from finresearch.fincalc.dates import to_ist, years_to_expiry

Right = Literal["call", "put", "future"]


# --------------------------------------------------------------------------- Black-76 on the forward
def b76_price(
    right: o.Right, forward: float, strike: float, t_years: float, rate: float, vol: float
) -> float:
    return o.bs_price(right, forward, strike, t_years, rate, vol, div_yield=rate)


def b76_greeks(
    right: o.Right, forward: float, strike: float, t_years: float, rate: float, vol: float
) -> o.Greeks:
    """Greeks with respect to the forward (delta = e^{-rT} N(d1) for a call); vega per vol point, theta per day."""
    return o.greeks(right, forward, strike, t_years, rate, vol, div_yield=rate)


def b76_iv(
    right: o.Right, price: float, forward: float, strike: float, t_years: float, rate: float
) -> float | None:
    """The Black-76 IV reproducing `price`; None outside the no-arbitrage bounds."""
    return o.implied_vol(right, price, forward, strike, t_years, rate, div_yield=rate)


def parity_forward(strike: float, call: float, put: float, t_years: float, rate: float) -> float:
    """The put-call parity forward from one strike's call and put: C - P = e^{-rT}(F - K)."""
    return strike + math.exp(rate * t_years) * (call - put)


# --------------------------------------------------------------------------- legs and the market
@dataclass(frozen=True)
class BookLeg:
    """One open leg: signed units (lots x lot size; + long, - short), its current mark and what the mark is ("mid"
    or "ltp"; None when unknown). A future's strike is ignored; its value is the forward of its own expiry."""

    underlying: str
    expiry: date
    right: Right
    strike: float
    qty: int
    mark: float | None
    mark_kind: str | None
    label: str = ""


@dataclass(frozen=True)
class Market:
    """The clock (#359), the rate, the forward per (underlying, expiry) (None or absent = unknown), betas of each
    underlying's front-future returns to NIFTY's (NIFTY = 1 always; absent = unknown) and NIFTY's level for the
    NIFTY-equivalent delta (None = unknown)."""

    now: datetime
    rate: float
    forwards: Mapping[tuple[str, date], float | None]
    betas: Mapping[str, float] = field(default_factory=dict)
    nifty: float | None = None

    def beta(self, underlying: str) -> float | None:
        return 1.0 if underlying.upper() == "NIFTY" else self.betas.get(underlying.upper())


@dataclass(frozen=True)
class LegState:
    """A leg priced now: its forward, T, own IV (options) and model value per unit; or `reason` why it cannot be."""

    leg: BookLeg
    forward: float | None = None
    t: float | None = None
    iv: float | None = None
    value: float | None = None
    greeks: o.Greeks | None = None
    reason: str | None = None
    flags: tuple[str, ...] = ()


def _price(x: BookLeg, m: Market) -> LegState:
    name = f"{x.underlying} {x.expiry:%d-%b-%Y} {x.right}{'' if x.right == 'future' else f' {x.strike:g}'}"
    t = years_to_expiry(x.expiry, m.now)
    if t <= 0:
        return LegState(x, reason=f"{name} has expired (15:30 IST on its expiry day): it cannot be repriced")
    f = m.forwards.get((x.underlying, x.expiry))
    if f is None or f <= 0:
        return LegState(
            x, t=t, reason=f"{name}: the forward (same-expiry future or parity forward) is unknown"
        )
    if x.right == "future":
        return LegState(x, f, t, None, f, o.Greeks(f, 1.0, 0.0, 0.0, 0.0, 0.0))
    if x.mark is None or x.mark <= 0:
        return LegState(x, f, t, reason=f"{name}: no price (mid or LTP) is known, so its IV is unknown")
    iv = b76_iv(x.right, x.mark, f, x.strike, t, m.rate)
    if iv is None:
        return LegState(x, f, t, reason=f"{name}: its price {x.mark:g} is outside the no-arbitrage bounds on the "
                                        f"forward {f:g}, so no IV reproduces it")  # fmt: skip
    g = b76_greeks(x.right, f, x.strike, t, m.rate, iv)
    flags = () if x.mark_kind == "mid" else (f"{name}: IV from the LTP (no two-sided quote), not a mid",)
    return LegState(x, f, t, iv, g.price, g, None, flags)


def price_legs(legs: Sequence[BookLeg], m: Market) -> list[LegState]:
    return [_price(x, m) for x in legs]


# --------------------------------------------------------------------------- exposures
BUCKETS = ("this_week", "this_month", "later")
WEEK_DAYS = 7  # an expiry within 7 calendar days is "this week" (the nearest weekly), even across a month end


def bucket(expiry: date, today: date) -> str:
    """The expiry bucket: this week (within WEEK_DAYS), this month (rest of the calendar month), or later."""
    if (expiry - today).days <= WEEK_DAYS:
        return "this_week"
    return "this_month" if (expiry.year, expiry.month) == (today.year, today.month) else "later"


@dataclass(frozen=True)
class Exposure:
    """₹ exposures: delta (₹ per 1 unit of relative move, Δ x F x q: ×0.01 for a 1 % move), gamma P&L of a 1 % move
    (½ Γ F² 0.01² q), vega per vol point and theta per calendar day."""

    rs_delta: float = 0.0
    rs_gamma_1pct: float = 0.0
    vega_pt: float = 0.0
    theta_day: float = 0.0

    def __add__(self, b: Exposure) -> Exposure:
        return Exposure(self.rs_delta + b.rs_delta, self.rs_gamma_1pct + b.rs_gamma_1pct, self.vega_pt + b.vega_pt,
                        self.theta_day + b.theta_day)  # fmt: skip


def leg_exposure(s: LegState) -> Exposure | None:
    if s.greeks is None or s.forward is None:
        return None
    g, f, q = s.greeks, s.forward, s.leg.qty
    return Exposure(g.delta * f * q, 0.5 * g.gamma * f * f * 1e-4 * q, g.vega * q, g.theta * q)


@dataclass(frozen=True)
class ExposureRow:
    underlying: str
    bucket: str
    exposure: Exposure | None  # None when any leg in it is unpriced
    legs: int


@dataclass(frozen=True)
class Exposures:
    """Per underlying x expiry bucket and in total; `total` and `nifty_delta` (NIFTY-equivalent units:
    Σ β_u Δ F_u q / NIFTY) are None while any leg is unpriced (`reasons`) or, for nifty_delta, a beta or NIFTY's
    level is unknown (`nifty_delta_reasons`)."""

    rows: tuple[ExposureRow, ...]
    total: Exposure | None
    nifty_delta: float | None
    reasons: tuple[str, ...]
    nifty_delta_reasons: tuple[str, ...]
    flags: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.reasons

    def row(self, underlying: str, bucket_: str) -> ExposureRow:
        return next(r for r in self.rows if r.underlying == underlying and r.bucket == bucket_)


def exposures(legs: Sequence[BookLeg], m: Market) -> Exposures:
    today = to_ist(m.now).date()
    states = price_legs(legs, m)
    groups: dict[tuple[str, str], list[LegState]] = {}
    for s in states:
        groups.setdefault((s.leg.underlying, bucket(s.leg.expiry, today)), []).append(s)
    rows, total, reasons = [], Exposure(), [s.reason for s in states if s.reason]
    nifty, nreasons = 0.0, []
    for (u, b), ss in sorted(groups.items(), key=lambda kv: (kv[0][0], BUCKETS.index(kv[0][1]))):
        es = [leg_exposure(s) for s in ss]
        e = None if any(x is None for x in es) else sum((x for x in es if x), Exposure())
        rows.append(ExposureRow(u, b, e, len(ss)))
        if e is not None:
            total += e
            beta = m.beta(u)
            if beta is None:
                nreasons.append(f"{u}: its beta to NIFTY is unknown")
            else:
                nifty += beta * e.rs_delta
    if m.nifty is None or m.nifty <= 0:
        nreasons.append("NIFTY's level is unknown")
    nreasons = sorted(set(nreasons)) + reasons
    return Exposures(tuple(rows), None if reasons else total, None if nreasons else nifty / m.nifty,  # type: ignore[operator]
                     tuple(reasons), tuple(nreasons), tuple(f for s in states for f in s.flags))  # fmt: skip


# --------------------------------------------------------------------------- full revaluation
# The grid and the measured NIFTY spot-vol response are #394's (reviewed roadmap item, 20-Oct-2026): ATM IV rises
# 1.04 vol points per 1 % fall on down days and falls 0.76 per 1 % rise on up days (NIFTY IV30 against index moves).
MOVES = (-0.15, -0.10, -0.07, -0.05, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10)
VOL_SHIFTS = (-3.0, 0.0, 5.0, 10.0, 20.0)  # vol points, on top of the spot-vol response
HORIZONS = (1, 5, "expiry")  # sessions; "expiry" = the close of the book's nearest expiry
DOWN_SLOPE, UP_SLOPE = 1.04, 0.76  # vol points per 1 % move
MIN_VOL = 0.01  # a shocked IV below 1 vol point is floored there (and counted): Black-76 needs sigma > 0


def vol_response(move: float) -> float:
    """The ATM IV change in vol points for a relative move of the underlying (sticky-strike: every strike shifts by
    it; the skew term dskew(m) x w(K) waits for the measured 25-delta skew response)."""
    pct = move * 100
    return -DOWN_SLOPE * pct if pct < 0 else -UP_SLOPE * pct


def horizon_clock(
    now: datetime, sessions: int, holidays: frozenset[date] | set[date] = frozenset()
) -> datetime:
    """15:30 IST on the `sessions`-th weekday after `now`'s date that is not in `holidays` (the NSE list, when the
    caller has it; weekdays only otherwise)."""
    from datetime import timedelta

    from finresearch.fincalc.dates import EXPIRY_CLOSE, ist_datetime

    d, n = to_ist(now).date(), 0
    while n < sessions:
        d += timedelta(days=1)
        n += d.weekday() < 5 and d not in holidays
    return ist_datetime(d, *EXPIRY_CLOSE)


@dataclass(frozen=True)
class Cell:
    """One scenario: the NIFTY move (other underlyings move β x it), the extra vol shift (points), the horizon and its
    clock, and the book's P&L in ₹ (None = incomplete: `reasons`). `floored`/`intrinsic` count legs whose shocked
    IV was floored at MIN_VOL and legs valued at intrinsic because they expire inside the horizon."""

    move: float
    dvol: float
    horizon: str
    at: datetime
    pnl: float | None
    reasons: tuple[str, ...] = ()
    floored: int = 0
    intrinsic: int = 0


def _intrinsic(right: str, f: float, k: float) -> float:
    return f if right == "future" else max(f - k, 0.0) if right == "call" else max(k - f, 0.0)


def _revalue(states: Sequence[LegState], m: Market, move: float, dvol: float, at: datetime, horizon: str,
             response: bool) -> Cell:  # fmt: skip
    reasons = [s.reason for s in states if s.reason]
    reasons += sorted({f"{s.leg.underlying}: its beta to NIFTY is unknown, so its move is unknown"
                       for s in states if m.beta(s.leg.underlying) is None})  # fmt: skip
    if reasons:
        return Cell(move, dvol, horizon, at, None, tuple(reasons))
    pnl, floored, intrinsic = 0.0, 0, 0
    for s in states:
        x, mu = s.leg, m.beta(s.leg.underlying) * move  # type: ignore[operator]
        f1, t1 = s.forward * (1 + mu), years_to_expiry(x.expiry, at)  # type: ignore[operator]
        if t1 <= 0 or x.right == "future":
            v1 = _intrinsic(x.right, f1, x.strike)
            intrinsic += t1 <= 0 and x.right != "future"
        else:
            vol = s.iv + ((vol_response(mu) if response else 0.0) + dvol) / 100  # type: ignore[operator]
            if vol < MIN_VOL:
                vol, floored = MIN_VOL, floored + 1
            v1 = b76_price(x.right, f1, x.strike, t1, m.rate, vol)
        pnl += (v1 - s.value) * x.qty  # type: ignore[operator]
    return Cell(move, dvol, horizon, at, pnl, (), floored, intrinsic)


def revalue(legs: Sequence[BookLeg], m: Market, move: float, dvol: float, at: datetime, *, response: bool = True,
            horizon: str = "") -> Cell:  # fmt: skip
    """PnL = Σ_j [V_j(F'_j, σ'_j, T_j at `at`) - V_j(now)] x q_j (Black-76 on the forward), F' = F(1 + β_u move),
    σ' = σ_K + response(β_u move) + dvol (points)."""
    return _revalue(price_legs(legs, m), m, move, dvol, at, horizon, response)


@dataclass(frozen=True)
class Grid:
    cells: tuple[Cell, ...]
    reasons: tuple[str, ...]  # why every cell is unknown (empty = complete)
    flags: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.reasons

    def cell(self, move: float, dvol: float, horizon: str) -> Cell:
        return next(c for c in self.cells if (c.move, c.dvol, c.horizon) == (move, dvol, horizon))


def grid(legs: Sequence[BookLeg], m: Market, moves: Sequence[float] = MOVES, vol_shifts: Sequence[float] = VOL_SHIFTS,
         horizons: Sequence[int | str] = HORIZONS, holidays: frozenset[date] | set[date] = frozenset()) -> Grid:  # fmt: skip
    """The full-revaluation ladder: every (move, vol shift, horizon), each leg repriced in full (no greek
    approximation). "expiry" is the close of the nearest expiry in the book (legs expiring then are at intrinsic)."""
    from finresearch.fincalc.dates import EXPIRY_CLOSE, ist_datetime

    states = price_legs(legs, m)
    clocks: list[tuple[str, datetime]] = []
    for h in horizons:
        if h == "expiry":
            if legs:
                clocks.append(("expiry", ist_datetime(min(x.expiry for x in legs), *EXPIRY_CLOSE)))
        else:
            clocks.append((f"{h}d", horizon_clock(m.now, int(h), holidays)))
    cells = [
        _revalue(states, m, mv, dv, at, name, True)
        for name, at in clocks
        for mv in moves
        for dv in vol_shifts
    ]
    reasons = cells[0].reasons if cells else ()
    return Grid(tuple(cells), reasons, tuple(f for s in states for f in s.flags))

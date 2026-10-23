"""Book-level exposures and a full-revaluation scenario engine for the F&O book (#394). Analysis only: nothing here
places, modifies or cancels an order. Pure arithmetic; the caller gathers legs, marks and forwards.

Every option leg is repriced with the app's own Black-76 IV, inverted from the leg's mark (its mid, or its LTP,
flagged, when there is no quote) on the forward of its own underlying and expiry: the same-expiry future, or the
put-call parity forward when there is none. Vendor IVs and fnobook's Black-Scholes greeks are not used. Black-76 is
`options.bs_price` with the forward as the spot and the dividend yield equal to the rate (the drift cancels:
e^{-rT}[F N(d1) - K N(d2)]), so the pricing code is the app's one implementation. T is to the minute, to 15:30 IST on
the expiry day (#359). A leg whose mark, forward or IV is unknown, or that has expired, is unpriced with a reason: the
book's exposures and scenarios are then incomplete and say so, never counted as 0.

#480 (the owner's decision of 23-Oct-2026): each expiry is discounted at its own rate (`Market.rates`, the caller's
signals.rates answer at that expiry's tenor), and a scenario rolls each forward to the horizon at its own expiry's carry
b = ln(F/S)/T (`Market.carries`: the market's dividend and financing, from the forward the legs are priced on and a
synchronous spot), F' = F (1 + m) e^{-b h}, never at the rate (which dropped NIFTY's dividend yield, about 0.85 index
points a day). An expiry whose carry is unknown leaves every scenario that lets time pass unknown, with the reason.
One flat carry per expiry spreads a discrete dividend over the whole life of the contract.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Literal

from finresearch.fincalc import beta as fbeta
from finresearch.fincalc import options as o
from finresearch.fincalc.beta import BetaEstimate
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


NO_CARRY = "the carry of its forward, ln(F/S)/T, is unknown"


@dataclass(frozen=True)
class Carry:
    """One expiry's carry b = ln(F/S)/T (annual, continuous), read from the forward F its legs are priced on, a
    synchronous spot S and T, with where it came from (`source`); `b` None = unknown, with `reason`."""

    b: float | None
    source: str | None = None
    reason: str | None = None
    forward: float | None = None
    spot: float | None = None
    t: float | None = None


def carry_from(forward: float | None, spot: float | None, t: float | None, source: str) -> Carry:
    """The carry of `forward` against a synchronous `spot` over `t` years (black76.carry), or unknown with why."""
    from finresearch.fincalc.black76 import carry

    if spot is None or not spot > 0:
        return Carry(None, None, f"the underlying's price to set against {source} is unknown", forward, spot, t)
    if forward is None or not forward > 0 or t is None or not t > 0:
        return Carry(None, None, f"{source}: no positive forward and time to expiry", forward, spot, t)
    return Carry(carry(forward, spot, t), source, None, forward, spot, t)


@dataclass(frozen=True)
class Market:
    """The clock (#359), the rate, the forward per (UNDERLYING, expiry) (upper case; None or absent = unknown), the
    registered beta estimate of each underlying's front-future returns to NIFTY's (fincalc.beta; NIFTY = 1 always;
    absent or with a reason = unknown) and NIFTY's level for the NIFTY-equivalent delta (None = unknown). #480: the
    carry per (UNDERLYING, expiry) the scenarios roll each forward at (absent = unknown, never the rate) and each
    expiry's own discount rate (absent = `rate`, the one rate a caller fixed)."""

    now: datetime
    rate: float
    forwards: Mapping[tuple[str, date], float | None]
    betas: Mapping[str, BetaEstimate] = field(default_factory=dict)
    nifty: float | None = None
    carries: Mapping[tuple[str, date], Carry] = field(default_factory=dict)
    rates: Mapping[date, float] = field(default_factory=dict)

    def fixed_carry(self, b: float, why: str) -> Market:
        """This market with every forward rolled at `b` on purpose: a registered study's or a model's own rule
        (joint-risk-v1's pinned rate; the card's S e^{rT} forward), labelled `why`. Never a fallback for an unknown
        carry."""
        return replace(self, carries={k: Carry(b, why) for k in self.forwards})

    def rate_for(self, expiry: date) -> float:
        """The expiry's own discount rate (#480: `rates`, continuous), else the one rate the caller fixed (`rate`)."""
        return self.rates.get(expiry, self.rate)

    def carry(self, underlying: str, expiry: date) -> Carry:
        """The expiry's carry; absent = unknown (never the rate)."""
        return self.carries.get((underlying.upper(), expiry)) or Carry(None, reason=NO_CARRY)

    def estimate(self, underlying: str) -> BetaEstimate | None:
        return fbeta.NIFTY if underlying.upper() == "NIFTY" else self.betas.get(underlying.upper())

    def beta(self, underlying: str) -> float | None:
        """The point estimate (the NIFTY-equivalent delta only: a display, no gate)."""
        e = self.estimate(underlying)
        return e.beta if e is not None and e.known else None

    def band(self, underlying: str) -> tuple[float, float] | None:
        """(lo, hi): every P&L under a NIFTY move takes the worse of the two ends (beta_specs.BetaSpec.band)."""
        e = self.estimate(underlying)
        return (e.lo, e.hi) if e is not None and e.known else None  # type: ignore[return-value]

    def beta_reason(self, underlying: str) -> str:
        e = self.estimate(underlying)
        return (
            e.reason if e is not None and e.reason else None
        ) or f"{underlying}: its beta to NIFTY is unknown"


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


def _name(x: BookLeg) -> str:
    return f"{x.underlying} {x.expiry:%d-%b-%Y} {x.right}{'' if x.right == 'future' else f' {x.strike:g}'}"


def _price(x: BookLeg, m: Market) -> LegState:
    name, r = _name(x), m.rate_for(x.expiry)
    t = years_to_expiry(x.expiry, m.now)
    if t <= 0:
        return LegState(x, reason=f"{name} has expired (15:30 IST on its expiry day): it cannot be repriced")
    f = m.forwards.get((x.underlying.upper(), x.expiry))
    if f is None or f <= 0:
        return LegState(
            x, t=t, reason=f"{name}: the forward (same-expiry future or parity forward) is unknown"
        )
    if x.right == "future":
        return LegState(x, f, t, None, f, o.Greeks(f, 1.0, 0.0, 0.0, 0.0, 0.0))
    if x.mark is None or x.mark <= 0:
        return LegState(x, f, t, reason=f"{name}: no price (mid or LTP) is known, so its IV is unknown")
    iv = b76_iv(x.right, x.mark, f, x.strike, t, r)
    if iv is None:
        return LegState(x, f, t, reason=f"{name}: its price {x.mark:g} is outside the no-arbitrage bounds on the "
                                        f"forward {f:g}, so no IV reproduces it")  # fmt: skip
    g = b76_greeks(x.right, f, x.strike, t, r, iv)
    flags = (
        ()
        if x.mark_kind == "mid"
        else (f"{name}: IV from the LTP (no two-sided quote), not a mid",)
        if x.mark_kind == "ltp"
        else (f"{name}: IV from a price of unknown kind (its source is not recorded)",)
    )
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
    # #397: the same sum in ₹ per unit relative NIFTY move (Σ β_u Δ F_u q), which needs no NIFTY level; None while
    # any leg is unpriced or a beta is unknown
    nifty_rs_delta: float | None = None

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
                nreasons.append(m.beta_reason(u))
            else:
                nifty += beta * e.rs_delta
    rs = None if reasons or nreasons else nifty
    if m.nifty is None or m.nifty <= 0:
        nreasons.append("NIFTY's level is unknown")
    nreasons = sorted(set(nreasons)) + reasons
    return Exposures(tuple(rows), None if reasons else total, None if nreasons else nifty / m.nifty,  # type: ignore[operator]
                     tuple(reasons), tuple(nreasons), tuple(f for s in states for f in s.flags), rs)  # fmt: skip


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
    ends: tuple[
        tuple[str, str], ...
    ] = ()  # per underlying with a band: which end ("lo" | "hi") was the worse


def _intrinsic(right: str, f: float, k: float) -> float:
    return f if right == "future" else max(f - k, 0.0) if right == "call" else max(k - f, 0.0)


def roll_reasons(states: Sequence[LegState], m: Market, at: datetime) -> list[str]:
    """#480: why priced legs cannot be rolled to `at`: their expiry's carry is unknown. One reason per underlying and
    expiry; none when no time passes (`at` is the clock)."""
    if at <= m.now:
        return []
    out: dict[tuple[str, date], int] = {}
    for s in states:
        if s.reason is None and m.carry(s.leg.underlying, s.leg.expiry).b is None:
            k = (s.leg.underlying.upper(), s.leg.expiry)
            out[k] = out.get(k, 0) + 1
    why = lambda c: f"{NO_CARRY} ({c.reason})" if c.reason and c.reason != NO_CARRY else NO_CARRY  # noqa: E731
    return [f"{u} {e:%d-%b-%Y} ({n} leg{'s' if n > 1 else ''}): scenario value unknown: {why(m.carry(u, e))}; a "
            "forward is never rolled at the risk-free rate" for (u, e), n in out.items()]  # fmt: skip


def _side(ss: Sequence[LegState], m: Market, mu: float, dvol: float, at: datetime,
          response: bool) -> tuple[float, int, int]:  # fmt: skip
    """One underlying's legs moved by mu: (P&L, IVs floored, legs at intrinsic)."""
    pnl, floored, intrinsic = 0.0, 0, 0
    for s in ss:
        x = s.leg
        # #480: the forward is shocked and rolled to the horizon at its own expiry's carry b = ln(F/S)/T, never at
        # the rate: F' = F (1 + mu) e^{-b (T - T')}, T' >= 0, so it keeps the market's dividend and financing, and at
        # its own expiry F' = S (1 + mu). No time passing needs no carry (_revalue refuses an unknown one otherwise).
        t1 = years_to_expiry(x.expiry, at)
        h = s.t - max(t1, 0.0)  # type: ignore[operator]
        f1 = s.forward * (1 + mu) * (math.exp(-m.carry(x.underlying, x.expiry).b * h) if h > 0 else 1.0)  # type: ignore[operator]
        if t1 <= 0 or x.right == "future":
            v1 = _intrinsic(x.right, f1, x.strike)
            intrinsic += t1 <= 0 and x.right != "future"
        else:
            vol = s.iv + ((vol_response(mu) if response else 0.0) + dvol) / 100  # type: ignore[operator]
            if vol < MIN_VOL:
                vol, floored = MIN_VOL, floored + 1
            v1 = b76_price(x.right, f1, x.strike, t1, m.rate_for(x.expiry), vol)
        pnl += (v1 - s.value) * x.qty  # type: ignore[operator]
    return pnl, floored, intrinsic


def _revalue(states: Sequence[LegState], m: Market, move: float, dvol: float, at: datetime, horizon: str,
             response: bool, own: Mapping[str, tuple[float, float]] | None = None) -> Cell:  # fmt: skip
    """Per underlying, its legs at the worse of its beta band's two ends (lo x move, hi x move), summed. An
    underlying in `own` ({UNDERLYING: (its own move, its own vol shift)}, historical scenarios) moves by that
    instead and needs no beta."""
    own = own or {}
    reasons = [s.reason for s in states if s.reason] + roll_reasons(states, m, at)
    groups: dict[str, list[LegState]] = {}
    for s in states:
        groups.setdefault(s.leg.underlying, []).append(s)
    reasons += sorted({m.beta_reason(u) for u in groups if u.upper() not in own and m.band(u) is None})
    if reasons:
        return Cell(move, dvol, horizon, at, None, tuple(reasons))
    pnl, floored, intrinsic, ends = 0.0, 0, 0, []
    for u in sorted(groups):
        if u.upper() in own:
            p, fl, it = _side(groups[u], m, *own[u.upper()], at, response)
            pnl, floored, intrinsic = pnl + p, floored + fl, intrinsic + it
            continue
        lo, hi = m.band(u)  # type: ignore[misc]
        sides = [(_side(groups[u], m, hi * move, dvol, at, response), "hi")]
        if lo != hi:
            sides.append((_side(groups[u], m, lo * move, dvol, at, response), "lo"))
            ends.append((u, min(sides, key=lambda x: x[0][0])[1]))
        (p, fl, it), _ = min(sides, key=lambda x: x[0][0])  # a tie keeps the upper end
        pnl, floored, intrinsic = pnl + p, floored + fl, intrinsic + it
    return Cell(move, dvol, horizon, at, pnl, (), floored, intrinsic, tuple(ends))


def revalue(legs: Sequence[BookLeg], m: Market, move: float, dvol: float, at: datetime, *, response: bool = True,
            horizon: str = "") -> Cell:  # fmt: skip
    """PnL = Σ_j [V_j(F'_j, σ'_j, T_j at `at`) - V_j(now)] x q_j (Black-76 on the forward, each expiry at its own
    rate), F' = F(1 + β_u move) e^{-b h} rolled at its expiry's carry b (#480),
    σ' = σ_K + response(β_u move) + dvol (points); β_u is the worse of the ends of u's band (lo, hi), per
    underlying (fincalc.beta_specs: the worse of the two ends, not the worst over the interval)."""
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


# --------------------------------------------------------------------------- historical single-day replays
@dataclass(frozen=True)
class Replay:
    day: date
    label: str
    move: float  # NIFTY's relative move that day
    iv30_change: float  # vol points
    source: str


def _log(r: float) -> float:
    return math.exp(r) - 1


# #394's table (reviewed roadmap item): 24-Feb-2022 is the front future's move, the later rows the index's log return
# (converted to a relative move here); IV30 changes in vol points, as measured in the issue.
REPLAYS = (
    Replay(date(2022, 2, 24), "24-Feb-2022 sell-off", -0.0488, 5.1, "NIFTY front future -4.88 %; IV30 +5.1"),
    Replay(date(2024, 6, 4), "4-Jun-2024 election-result day", _log(-0.0611), 4.46,
           "NIFTY index log return -6.11 % (the front future fell 6.41 %); IV30 +4.46"),
    Replay(date(2024, 8, 5), "5-Aug-2024 sell-off", _log(-0.0272), 5.42, "NIFTY index log return -2.72 %; IV30 +5.42"),
    Replay(date(2025, 4, 7), "7-Apr-2025 sell-off", _log(-0.0330), 9.60, "NIFTY index log return -3.30 %; IV30 +9.60"),
)  # fmt: skip
SKEW_NOTE = ("The same expiry's 25-delta skew change (from fno_bhav) is not applied yet: every strike's IV moves by the "
             "day's IV30 change.")  # fmt: skip


@dataclass(frozen=True)
class ReplayResult:
    replay: Replay
    cell: Cell
    notes: tuple[str, ...]


def replays(legs: Sequence[BookLeg], m: Market, holidays: frozenset[date] | set[date] = frozenset()) -> list[ReplayResult]:  # fmt: skip
    """Each day of REPLAYS applied to today's book over one session: NIFTY's move (other underlyings β x it, noted:
    not their own move that day), every IV shifted by the day's IV30 change (not the response rule)."""
    states, at = price_legs(legs, m), horizon_clock(m.now, 1, holidays)
    mapped = sorted({s.leg.underlying for s in states if s.leg.underlying.upper() != "NIFTY"})
    notes = [
        SKEW_NOTE,
        *(
            f"{u} moves by its beta to NIFTY (the worse end of its band) x NIFTY's move, not by its own move that day"
            for u in mapped
        ),
    ]
    return [
        ReplayResult(r, _revalue(states, m, r.move, r.iv30_change, at, "1d", False), tuple(notes))
        for r in REPLAYS
    ]


# --------------------------------------------------------------------------- reverse stress
def book_limit(capital: float) -> float:
    """The book loss limit: the open-book budget, BOOK_BUDGET_PCT of F&O capital (#358)."""
    from finresearch.fincalc.tradeplan import BOOK_BUDGET_PCT

    return capital * BOOK_BUDGET_PCT / 100


@dataclass(frozen=True)
class Reverse:
    """Per horizon, the smallest grid point whose loss reaches `limit`: smallest |move| first, then smallest |vol
    shift|, then a fall before a rise. cell None + complete = no grid point breaches; incomplete = unknown."""

    horizon: str
    limit: float
    cell: Cell | None
    reasons: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.reasons


def reverse_stress(g: Grid, limit: float) -> list[Reverse]:
    if limit <= 0:
        raise ValueError("the book loss limit must be positive")
    out = []
    for h in dict.fromkeys(c.horizon for c in g.cells):
        cs = sorted(
            (c for c in g.cells if c.horizon == h), key=lambda c: (abs(c.move), abs(c.dvol), c.move, c.dvol)
        )
        if not g.complete:
            out.append(Reverse(h, limit, None, g.reasons))
            continue
        out.append(
            Reverse(h, limit, next((c for c in cs if c.pnl is not None and c.pnl <= -limit), None), ())
        )
    return out


# --------------------------------------------------------------------------- the common-shock term (#395)
def shock_pnls(legs: Sequence[BookLeg], m: Market, at: datetime,
               moves: Sequence[float] | None = None) -> tuple[dict[float, float] | None, tuple[str, ...]]:  # fmt: skip
    """P4.2's common-shock loss L(m) = Σ_j PnL_j(F_j (1 + β_j m), σ_j + dσ(β_j m), T at `at`) for each registered
    NIFTY move (fincalc.tradeplan.SHOCK_MOVES): the full revaluation above with the spot-vol response and no extra vol
    shift, `at` one session later, each underlying at the worse end of its beta band. (None, reasons) when any leg is unpriced or its beta is unknown, never 0."""
    from finresearch.fincalc.tradeplan import SHOCK_MOVES

    states = price_legs(legs, m)
    out: dict[float, float] = {}
    for mv in SHOCK_MOVES if moves is None else moves:
        c = _revalue(states, m, mv, 0.0, at, "shock", True)
        if c.pnl is None:
            return None, c.reasons
        out[mv] = c.pnl
    return out, ()

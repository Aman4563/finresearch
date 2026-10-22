"""Post-trade review arithmetic (#383, P2.8): implementation shortfall against the card, the daily P&L explain by
greeks and by full revaluation, the residual acceptance test, plan adherence and the exposure mix. Pure functions on
plain numbers; `fnobook.review` reads the rows. Analysis only: nothing here places an order.

Conventions (the issue's reviewed text):
* IS = Σ legs side × (fill − mid at the card) × qty + fees, side = +1 buy, −1 sell (positive = a cost), in ₹ and as %
  of |the net premium at the card's mids| (the credit of a credit trade, the debit of a debit trade).
* dV ≈ Δ dS + ½ Γ dS² + vega dσ + Θ dt + residual, greeks at the previous mark (fincalc.options units: vega per 1 vol
  point, theta per calendar day, so dσ is in points and dt in calendar days); U = |residual| ÷ |actual|.
* Acceptance (T1): |residual| ≤ max(10 % of |actual|, 0.5 % of the position's max loss), because the ratio alone fails
  mechanically on near-zero P&L days.
Unknown inputs give unknown (None) outputs, never 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

RATIO_TOL = 0.10  # 10 % of |actual|
MAX_LOSS_FLOOR = 0.005  # 0.5 % of the position's max loss


@dataclass(frozen=True)
class FillLeg:
    side: str  # buy | sell
    qty: int  # units, unsigned
    fill: float
    mid: float | None  # the card's mid for this leg; None = unknown


@dataclass(frozen=True)
class Shortfall:
    price: float | None  # Σ side × (fill − mid) × qty, ₹
    fees: float | None
    total: float | None
    base: float | None  # |net premium at the mids|, ₹
    pct: float | None
    why: str | None = None


def _sign(side: str) -> int:
    return 1 if side == "buy" else -1


def shortfall(legs: Sequence[FillLeg], fees: float | None) -> Shortfall:
    """Implementation shortfall of one trade's entry against the card's mids (module docstring)."""
    if any(x.mid is None for x in legs):
        return Shortfall(None, fees, None, None, None, "a leg has no mid at the card")
    price = sum(_sign(x.side) * (x.fill - x.mid) * x.qty for x in legs)  # type: ignore[operator]
    base = abs(sum(-_sign(x.side) * x.mid * x.qty for x in legs))  # type: ignore[operator]
    if fees is None:
        return Shortfall(price, None, None, base, None, "the fees are unknown")
    total = price + fees
    return Shortfall(price, fees, total, base, total / base * 100 if base else None)


@dataclass(frozen=True)
class PositionGreeks:
    """A position's greeks in ₹ (per-unit greeks × signed units): vega per vol point, theta per calendar day."""

    delta: float
    gamma: float
    vega: float
    theta: float


@dataclass(frozen=True)
class Explain:
    delta: float
    gamma: float
    vega: float
    theta: float
    explained: float
    actual: float
    residual: float
    unexplained: float | None  # U = |residual| ÷ |actual|; None when actual is 0


def greek_explain(
    g: PositionGreeks, *, d_spot: float, d_vol_pts: float, d_days: float, actual: float
) -> Explain:
    d, gm = g.delta * d_spot, 0.5 * g.gamma * d_spot * d_spot
    v, th = g.vega * d_vol_pts, g.theta * d_days
    explained = d + gm + v + th
    res = actual - explained
    return Explain(d, gm, v, th, explained, actual, res, abs(res) / abs(actual) if actual else None)


def within_tolerance(*, residual: float | None, actual: float | None, max_loss: float | None) -> bool | None:
    """|residual| ≤ max(10 % |actual|, 0.5 % |max loss|). None when it cannot be decided: the residual is unknown, or
    the ratio fails and the max loss (the floor) is unknown or unbounded."""
    if residual is None or actual is None:
        return None
    if abs(residual) <= RATIO_TOL * abs(actual):
        return True
    if max_loss is None:
        return None
    return abs(residual) <= MAX_LOSS_FLOOR * abs(max_loss)


@dataclass(frozen=True)
class MarkLeg:
    """One leg between two marks: signed units (+ long, − short) and its IV at each mark (a fraction; None = unknown)."""

    right: str  # call | put | future
    strike: float
    qty: int
    iv0: float | None
    iv1: float | None


def _value(right: str, strike: float, spot: float, t: float, rate: float, vol: float | None) -> float | None:
    """Model value per unit: a future at the spot, an option at intrinsic once T ≤ 0, else Black-Scholes."""
    from finresearch.fincalc import options as o

    if right == "future":
        return spot
    if t <= 0:
        return max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
    if vol is None or vol <= 0:
        return None
    return o.bs_price(right, spot, strike, t, rate, vol)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Reval:
    spot: float  # V(S1, σ0, T0) − V(S0, σ0, T0)
    vol: float  # V(S1, σ1, T0) − V(S1, σ0, T0)
    time: float  # V(S1, σ1, T1) − V(S1, σ1, T0)
    model: float  # their sum = V(S1, σ1, T1) − V(S0, σ0, T0)
    actual: float
    residual: (
        float  # actual − model: the marks against the model (non-zero when a mark is not the model price)
    )


def revaluation(legs: Sequence[MarkLeg], *, s0: float, s1: float, t0: float, t1: float, rate: float,
                actual: float) -> Reval | None:  # fmt: skip
    """The day's P&L by full revaluation, sequentially spot → vol → time. At expiry (T1 ≤ 0) the IV is unknown and
    irrelevant: σ1 is held at σ0, so the vol step is 0. None when a needed IV is unknown."""
    sp = vo = ti = 0.0
    for x in legs:
        v1 = x.iv1 if t1 > 0 else x.iv0
        if x.right != "future" and (x.iv0 is None or v1 is None):
            return None
        a = _value(x.right, x.strike, s0, t0, rate, x.iv0)
        b = _value(x.right, x.strike, s1, t0, rate, x.iv0)
        c = _value(x.right, x.strike, s1, t0, rate, v1)
        d = _value(x.right, x.strike, s1, t1, rate, v1)
        if a is None or b is None or c is None or d is None:
            return None
        sp, vo, ti = sp + x.qty * (b - a), vo + x.qty * (c - b), ti + x.qty * (d - c)
    model = sp + vo + ti
    return Reval(sp, vo, ti, model, actual, actual - model)


def position_greeks(legs: Sequence[MarkLeg], *, spot: float, t: float, rate: float) -> PositionGreeks | None:
    """Σ per-unit greeks × signed units at the first mark (σ0). None when T ≤ 0 or an option's IV is unknown."""
    from finresearch.fincalc import options as o

    if t <= 0:
        return None
    d = g = v = th = 0.0
    for x in legs:
        if x.right == "future":
            d += x.qty
            continue
        if x.iv0 is None or x.iv0 <= 0:
            return None
        k = o.greeks(x.right, spot, x.strike, t, rate, x.iv0)  # type: ignore[arg-type]
        d, g, v, th = d + k.delta * x.qty, g + k.gamma * x.qty, v + k.vega * x.qty, th + k.theta * x.qty
    return PositionGreeks(d, g, v, th)


# --------------------------------------------------------------------------- plan adherence
# A planned exit is a square-off within PLANNED_WINDOW of the first exit rule that fired (profit → target, stop → stop,
# dte → time, delta/event → risk), or a settlement at expiry when no rule fired (the plan's profit and stop were never
# reached) or the card's plan held to expiry. A square-off after the window is "late"; one with rules armed but none
# fired is "discretionary"; a trade with neither rules nor a card plan has no plan and is left out of the rate.
PLANNED_WINDOW_HOURS = 24  # [U: a convention; a decision for the owner]
_PLANNED = {"profit": "planned_target", "stop": "planned_stop", "dte": "planned_time", "delta": "planned_risk",
            "event": "planned_risk"}  # fmt: skip
CLASSES = (
    "planned_target",
    "planned_stop",
    "planned_time",
    "planned_risk",
    "late",
    "discretionary",
    "no_plan",
)


@dataclass(frozen=True)
class RuleState:
    kind: str
    threshold: float
    fired_at: datetime | None


@dataclass(frozen=True)
class ClosedTrade:
    reason: str  # square_off | expiry
    closed_at: datetime
    gross_pnl: float | None
    base: float | None  # |entry net option premium|, ₹ (exits.read's base)
    rules: Sequence[RuleState]
    hold_to_expiry: bool | None = None  # the linked card's plan; None = no card


def classify(t: ClosedTrade) -> str:
    fired = sorted((r for r in t.rules if r.fired_at is not None and r.fired_at <= t.closed_at),
                   key=lambda r: r.fired_at)  # type: ignore[arg-type,return-value]  # fmt: skip
    if fired:
        first = fired[0]
        late = t.closed_at - first.fired_at > timedelta(hours=PLANNED_WINDOW_HOURS)  # type: ignore[operator]
        return "late" if late else _PLANNED[first.kind]
    if t.reason == "expiry" and (t.rules or t.hold_to_expiry):
        return "planned_time"
    if not t.rules and not t.hold_to_expiry:
        return "no_plan"
    return "discretionary"


def beyond_stop(t: ClosedTrade) -> float | None:
    """₹ lost beyond the stop: max(0, −gross − stop % × base); None without a stop rule or a known P&L and base."""
    stops = [r for r in t.rules if r.kind == "stop"]
    if not stops or t.gross_pnl is None or not t.base:
        return None
    return max(0.0, -t.gross_pnl - min(r.threshold for r in stops) / 100 * t.base)


@dataclass(frozen=True)
class Adherence:
    counts: dict[str, int]
    rate: float | None  # planned ÷ (planned + late + discretionary)
    with_stop: int
    beyond_stop_count: int
    mean_beyond_stop: float | None  # over the trades that went beyond their stop


def adherence(trades: Sequence[ClosedTrade]) -> Adherence:
    counts: dict[str, int] = {}
    for t in trades:
        c = classify(t)
        counts[c] = counts.get(c, 0) + 1
    planned = sum(v for k, v in counts.items() if k.startswith("planned_"))
    judged = planned + counts.get("late", 0) + counts.get("discretionary", 0)
    stops = [b for t in trades if (b := beyond_stop(t)) is not None]
    over = [b for b in stops if b > 0]
    return Adherence(counts, planned / judged if judged else None, len(stops), len(over),
                     sum(over) / len(over) if over else None)  # fmt: skip


# --------------------------------------------------------------------------- exposure mix
@dataclass(frozen=True)
class MixTrade:
    opened: date  # IST day
    expiry: date
    legs: Sequence[tuple[str, float, int]]  # (side, entry price, units) of the option legs
    held_days: int | None  # calendar days open; None while open


@dataclass(frozen=True)
class Mix:
    trades: int
    premium_sold_share: float | None
    same_day_expiry_share: float | None
    mean_hold_share: float | None  # mean of days held ÷ days to expiry at entry (closed trades with DTE > 0)


def exposure_mix(trades: Sequence[MixTrade]) -> Mix:
    sold = sum(p * q for t in trades for s, p, q in t.legs if s == "sell")
    total = sum(p * q for t in trades for _, p, q in t.legs)
    holds = [t.held_days / (t.expiry - t.opened).days for t in trades
             if t.held_days is not None and (t.expiry - t.opened).days > 0]  # fmt: skip
    n = len(trades)
    return Mix(n, sold / total if total else None, sum(t.opened == t.expiry for t in trades) / n if n else None,
               sum(holds) / len(holds) if holds else None)  # fmt: skip

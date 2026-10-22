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


def greek_explain(g: PositionGreeks, *, d_spot: float, d_vol_pts: float, d_days: float, actual: float) -> Explain:
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

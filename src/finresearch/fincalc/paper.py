"""Paper-trading arithmetic for F&O strategies (#308): fills with slippage, mark-to-market, square-off and expiry
settlement with costs, and the book's results. Analysis only: nothing here places an order.

Conventions (the same as `fincalc.options` and `fincalc.charges`: floats in rupees, quantities in units = lots × lot
size, positive for the units bought or sold):
* Fill: a buy fills at the reference price × (1 + slippage), a sell at × (1 − slippage), rounded to the paisa. The
  reference is Groww's last traded price: Groww's option chain carries no bid or ask, so the LTP is the ask proxy
  and the slippage stands for the spread and impact.
* P&L of a leg = (price now − fill) × units, negated for a sold leg. Gross = before costs; net = after entry costs
  and the exit's costs (square-off charges, or exercise STT at expiry).
* Expiry settlement: an option is worth its intrinsic value at the settlement price, a future the settlement price.
  STT on exercise (`charges.exercise_stt`) is charged on the intrinsic value of long options that finish in the money;
  the writer pays none. No exchange charges, stamp duty or brokerage are modelled on settlement itself [U: brokers
  differ; some charge for physically settled stock F&O, which is not modelled].
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from finresearch.fincalc import charges as ch

Right = Literal["call", "put", "future"]


@dataclass(frozen=True)
class PaperLeg:
    right: Right
    strike: float  # the future's strike is ignored (its fill is its entry)
    side: ch.Side
    qty: int  # units (lots × lot size), > 0
    entry: float  # fill price per unit

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1


def fill_price(reference: float, side: ch.Side, slippage_pct: float) -> float:
    """The paper fill: the reference price moved against the trader by `slippage_pct` (%), to the paisa."""
    if reference is None or reference <= 0:
        raise ValueError("no reference price to fill at")
    if not 0 <= slippage_pct < 100:
        raise ValueError("slippage must be from 0 to 100 %")
    f = 1 + slippage_pct / 100 if side == "buy" else 1 - slippage_pct / 100
    return float(Decimal(str(reference * f)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _trade_legs(legs: Sequence[PaperLeg], prices: Sequence[float], flip: bool = False) -> list[ch.TradeLeg]:
    out = []
    for x, p in zip(legs, prices, strict=True):
        side: ch.Side = ("sell" if x.side == "buy" else "buy") if flip else x.side
        out.append(ch.TradeLeg("future" if x.right == "future" else "option", side, p, x.qty))
    return out


def entry_costs(legs: Sequence[PaperLeg], on: date, brokerage: float,
                overrides: Mapping[str, Decimal | float | str] | None = None) -> ch.Costs:  # fmt: skip
    return ch.order_costs(_trade_legs(legs, [x.entry for x in legs]), on, brokerage, overrides)


def square_off_costs(legs: Sequence[PaperLeg], prices: Sequence[float], on: date, brokerage: float,
                     overrides: Mapping[str, Decimal | float | str] | None = None) -> ch.Costs:  # fmt: skip
    """The charges of closing every leg at `prices` (one order per leg)."""
    return ch.order_costs(_trade_legs(legs, prices, flip=True), on, brokerage, overrides)


def gross_pnl(legs: Sequence[PaperLeg], prices: Sequence[float | None]) -> float | None:
    """Mark-to-market P&L before costs; None when any leg has no price (never a price of 0)."""
    if any(p is None for p in prices):
        return None
    return sum((p - x.entry) * x.qty * x.sign for x, p in zip(legs, prices, strict=True))  # type: ignore[operator]


def intrinsic(right: Right, strike: float, price: float) -> float:
    if right == "future":
        return price
    return max(0.0, price - strike) if right == "call" else max(0.0, strike - price)


@dataclass(frozen=True)
class Settlement:
    settlement_price: float
    final_values: list[float]  # per unit, per leg
    gross: float
    exercise_stt: float
    itm_long_intrinsic: float  # the base of exercise STT (₹)


def settle(legs: Sequence[PaperLeg], settlement_price: float, expiry: date,
           overrides: Mapping[str, Decimal | float | str] | None = None) -> Settlement:  # fmt: skip
    """Expiry settlement at `settlement_price` (see the module docstring)."""
    if settlement_price is None or settlement_price <= 0:
        raise ValueError("no settlement price")
    finals = [intrinsic(x.right, x.strike, settlement_price) for x in legs]
    gross = sum((v - x.entry) * x.qty * x.sign for x, v in zip(legs, finals, strict=True))
    base = sum(
        v * x.qty for x, v in zip(legs, finals, strict=True) if x.right != "future" and x.side == "buy"
    )
    notional = sum(settlement_price * x.qty for x, v in zip(legs, finals, strict=True)
                   if x.right != "future" and x.side == "buy" and v > 0)  # the pre-Sep-2019 STT base (#365)  # fmt: skip
    return Settlement(settlement_price, finals, gross,
                      ch.exercise_stt(base, expiry, overrides, settlement_value=notional), base)  # fmt: skip


def net_premium(legs: Sequence[PaperLeg]) -> float:
    """Option premium paid (+) or received (−) at entry, in ₹; futures carry none."""
    return sum(x.entry * x.qty * x.sign for x in legs if x.right != "future")


# --------------------------------------------------------------------------- the book's results
@dataclass(frozen=True)
class Results:
    closed: int
    wins: int
    losses: int
    win_rate: float | None
    expectancy: float | None  # mean net P&L per closed trade (₹)
    avg_win: float | None
    avg_loss: float | None
    total: float
    max_drawdown: float  # the largest fall of the cumulative net P&L from its running peak (₹, ≥ 0), from 0
    curve: list[float]


def results(net_pnls_in_close_order: Sequence[float]) -> Results:
    """Every closed trade counts (a break-even trade is neither a win nor a loss but is in the denominator)."""
    xs = list(net_pnls_in_close_order)
    wins, losses = [x for x in xs if x > 0], [x for x in xs if x < 0]
    curve, run, peak, dd = [], 0.0, 0.0, 0.0
    for x in xs:
        run += x
        curve.append(run)
        peak = max(peak, run)
        dd = max(dd, peak - run)
    n = len(xs)
    return Results(n, len(wins), len(losses), len(wins) / n if n else None, sum(xs) / n if n else None,
                   sum(wins) / len(wins) if wins else None, sum(losses) / len(losses) if losses else None,
                   run, dd, curve)  # fmt: skip

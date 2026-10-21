"""An APPROXIMATE, SPAN-style margin for an F&O basket (#308). Not the exchange's margin.

Why approximate: the exchange's SPAN risk-parameter files are not read by the app. Groww's basket margin calculator
(``POST /v1/margins/detail/orders``, the one calculator POST the read-only client may send, #316) gives the broker's
figure when Groww is connected and answers; this estimate is the fallback (no Groww, an error, a contract Groww's
instruments file does not list) and is always shown, labelled, with its assumptions:

* Scan risk: the basket is repriced (Black-Scholes at each leg's IV, the same time to expiry) under price moves of
  0, ±1/3, ±2/3 and ±1 × the price-scan range, each with the volatility shifted up and down by the vol-scan range,
  plus the two extreme moves (±2 × the range) counted at 35 % (the SPAN method's 16-scenario shape). The scan risk is
  the worst loss across the scenarios (≥ 0).
* Exposure margin: a percentage of the notional (strike × units for short options, price × units for futures).
* Long-only option baskets need the premium only (no margin).
* The range and exposure percentages below are assumptions [U], not values read from an exchange file or circular:
  real ranges change daily per underlying. Pass `params` to try others.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from finresearch.fincalc import options as o

# assumptions [U]: (price scan range, vol scan range in vol points as a fraction, exposure % of notional)
PARAMS: dict[str, dict[str, float]] = {
    "index": {"price_scan": 0.06, "vol_scan": 0.04, "exposure": 0.02},
    "stock": {"price_scan": 0.12, "vol_scan": 0.04, "exposure": 0.035},
}
STATUS = "unconfirmed"
LABEL = "approximate: SPAN-style estimate (exchange SPAN files not read); the fallback when Groww's margin calculator is unavailable"
MOVES = (0.0, 1 / 3, -1 / 3, 2 / 3, -2 / 3, 1.0, -1.0)
EXTREME, EXTREME_WEIGHT = 2.0, 0.35


@dataclass(frozen=True)
class MarginLeg:
    right: str  # call | put | future
    strike: float  # a future's entry price
    qty: int  # signed units: + long, − short
    price: float  # premium now (options) or futures price
    iv: float | None  # annual, fraction; required for options


@dataclass(frozen=True)
class MarginEstimate:
    scan_risk: float
    exposure: float
    premium_paid: float
    total: float
    worst_move: float  # the price move (fraction) of the worst scenario
    params: dict[str, float]


def _value(legs: Sequence[MarginLeg], spot: float, t: float, rate: float, dv: float) -> float:
    v = 0.0
    for x in legs:
        if x.right == "future":
            v += spot * x.qty
        else:
            v += o.bs_price(x.right, spot, x.strike, t, rate, max(0.01, x.iv + dv)) * x.qty  # type: ignore[arg-type,operator]
    return v


def estimate(legs: Sequence[MarginLeg], spot: float, t_years: float, rate: float, kind: str,
             params: dict[str, float] | None = None) -> MarginEstimate:  # fmt: skip
    """`kind` "index" or "stock" picks the default assumptions; `t_years` to the minute (#359). Raises ValueError when an
    option leg has no IV or the basket has expired."""
    p = {**PARAMS[kind], **(params or {})}
    if any(x.right != "future" and not x.iv for x in legs):
        raise ValueError("an option leg has no implied volatility: no margin estimate")
    if t_years <= 0:  # #359: T is to the minute and never floored; an expired basket has no margin to estimate
        raise ValueError("the contract has expired (15:30 IST on the expiry day): no margin estimate")
    t = t_years
    premium = sum(x.price * x.qty for x in legs if x.right != "future" and x.qty > 0)
    shorts = [x for x in legs if x.qty < 0]
    if not shorts and all(x.right != "future" for x in legs):
        return MarginEstimate(0.0, 0.0, premium, premium, 0.0, p)
    base = _value(legs, spot, t, rate, 0.0)
    worst, worst_move = 0.0, 0.0
    scen = [(m, 1.0) for m in MOVES] + [(EXTREME, EXTREME_WEIGHT), (-EXTREME, EXTREME_WEIGHT)]
    for m, w in scen:
        for dv in (p["vol_scan"], -p["vol_scan"]):
            loss = (base - _value(legs, spot * (1 + m * p["price_scan"]), t, rate, dv)) * w
            if loss > worst:
                worst, worst_move = loss, m * p["price_scan"]
    notional = sum((x.strike if x.right != "future" else spot) * abs(x.qty) for x in legs
                   if x.qty < 0 or x.right == "future")  # fmt: skip
    exposure = notional * p["exposure"]
    return MarginEstimate(worst, exposure, premium, worst + exposure + premium, worst_move, p)


def to_json(e: MarginEstimate, kind: str) -> dict[str, Any]:
    return {"status": "approximate", "label": LABEL, "scan_risk": round(e.scan_risk, 2),
            "exposure": round(e.exposure, 2), "premium_paid": round(e.premium_paid, 2), "total": round(e.total, 2),
            "worst_move_pct": round(e.worst_move * 100, 2), "kind": kind,
            "assumptions": {k: {"value": v, "status": STATUS} for k, v in e.params.items()},
            "note": "Brokers' required margin can differ a lot (daily exchange parameters, calendar spreads, "
                    "expiry-day add-ons). Use Groww's own margin screen before trading."}  # fmt: skip

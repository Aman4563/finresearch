"""An APPROXIMATE, SPAN-style margin for an F&O basket (#308). Not the exchange's margin.

Why approximate: the full SPAN computation (risk arrays per contract, spreads, the short-option minimum) is not done
here (P4.3). Since #370 the price and vol scan ranges come from NSE's SPAN file for the day when one is stored
(fnohist.span.params_for; the caller passes them as `params` with their `sources`); each assumption is labelled with
its source, and anything not from a file stays "unconfirmed". Groww's basket margin calculator
(``POST /v1/margins/detail/orders``, the one calculator POST the read-only client may send, #316) gives the broker's
figure when Groww is connected and answers; this estimate is the fallback (no Groww, an error, a contract Groww's
instruments file does not list) and is always shown, labelled, with its assumptions:

* Scan risk: the basket is repriced (Black-Scholes at each leg's IV, the same time to expiry) under price moves of
  0, ±1/3, ±2/3 and ±1 × the price-scan range, each with the volatility shifted up and down by the vol-scan range,
  plus the two extreme moves (±2 × the range) counted at 35 % (the SPAN method's 16-scenario shape). The scan risk is
  the worst loss across the scenarios (≥ 0).
* Exposure margin: a percentage of the notional (strike × units for short options, price × units for futures).
* Long-only option baskets need the premium only (no margin).
* Without a stored file the ranges below are assumptions [U]; real ranges change daily per underlying (16-Oct-2026:
  NIFTY and BANKNIFTY 9.30 %, stocks 14-20 %, against the assumed 6 % / 12 %). The exposure (ELM) percentage is not
  in the SPAN file (NSE's 'ael_' file name answered 404), so it stays an assumption [U] until read from circulars.
* NSCCL's own extreme scenarios are ±3 ranges at weight 0.30 (the file's pointDef); this estimate keeps ±2 at 0.35
  [U] (a decision for P4.3, which reads the risk arrays themselves).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from finresearch.fincalc import options as o

# assumptions [U]: (price scan range, vol scan range in vol points as a fraction, exposure % of notional)
PARAMS: dict[str, dict[str, float]] = {
    "index": {"price_scan": 0.06, "vol_scan": 0.04, "exposure": 0.02},
    "stock": {"price_scan": 0.12, "vol_scan": 0.04, "exposure": 0.035},
}
STATUS = "unconfirmed"
LABEL = "approximate: SPAN-style estimate with assumed scan ranges (no NSE SPAN file stored for this day); the fallback when Groww's margin calculator is unavailable"
LABEL_SPAN = "approximate: SPAN-style estimate using the scan ranges of the day's NSE SPAN file (exposure % assumed); the fallback when Groww's margin calculator is unavailable"
TRIGGER_MEDIAN_PCT, TRIGGER_P95_PCT = 5.0, 15.0  # MF16: build the full margin engine (P4.3) above either
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
    sources: dict[str, str] = field(default_factory=dict)  # param -> where its value came from (else assumed)


def _value(legs: Sequence[MarginLeg], spot: float, t: float, rate: float, dv: float) -> float:
    v = 0.0
    for x in legs:
        if x.right == "future":
            v += spot * x.qty
        else:
            v += o.bs_price(x.right, spot, x.strike, t, rate, max(0.01, x.iv + dv)) * x.qty  # type: ignore[arg-type,operator]
    return v


def estimate(legs: Sequence[MarginLeg], spot: float, t_years: float, rate: float, kind: str,
             params: dict[str, float] | None = None, sources: dict[str, str] | None = None) -> MarginEstimate:  # fmt: skip
    """`kind` "index" or "stock" picks the default assumptions; `params` overrides them and `sources` says where each
    override came from (e.g. "NSE SPAN file 2026-10-16"). Raises ValueError when an option leg has no IV."""
    p = {**PARAMS[kind], **(params or {})}
    src = {k: v for k, v in (sources or {}).items() if k in p}
    if any(x.right != "future" and not x.iv for x in legs):
        raise ValueError("an option leg has no implied volatility: no margin estimate")
    t = max(t_years, 0.5 / 365)
    premium = sum(x.price * x.qty for x in legs if x.right != "future" and x.qty > 0)
    shorts = [x for x in legs if x.qty < 0]
    if not shorts and all(x.right != "future" for x in legs):
        return MarginEstimate(0.0, 0.0, premium, premium, 0.0, p, src)
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
    return MarginEstimate(worst, exposure, premium, worst + exposure + premium, worst_move, p, src)


def to_json(e: MarginEstimate, kind: str) -> dict[str, Any]:
    return {"status": "approximate", "label": LABEL_SPAN if e.sources else LABEL, "scan_risk": round(e.scan_risk, 2),
            "exposure": round(e.exposure, 2), "premium_paid": round(e.premium_paid, 2), "total": round(e.total, 2),
            "worst_move_pct": round(e.worst_move * 100, 2), "kind": kind,
            "assumptions": {k: {"value": v, "status": e.sources.get(k, STATUS)} for k, v in e.params.items()},
            "note": "Brokers' required margin can differ a lot (daily exchange parameters, calendar spreads, "
                    "expiry-day add-ons). Use Groww's own margin screen before trading."}  # fmt: skip


def check_summary(rows: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """[(approx margin, Groww margin)] like for like -> the MF16 trigger for the full margin engine (P4.3): build it
    when the median |approx - Groww| / Groww is above 5 % or the 95th percentile (nearest rank) above 15 %. No
    usable rows: the trigger is unknown (None), never "no"."""
    pct = sorted(abs(a - g) / g * 100 for a, g in rows if g and g > 0)
    if not pct:
        return {"n": 0, "median_abs_pct": None, "p95_abs_pct": None, "build_full_engine": None}
    import math
    import statistics

    med = statistics.median(pct)
    p95 = pct[max(0, math.ceil(0.95 * len(pct)) - 1)]
    return {"n": len(pct), "median_abs_pct": med, "p95_abs_pct": p95,
            "build_full_engine": med > TRIGGER_MEDIAN_PCT or p95 > TRIGGER_P95_PCT}  # fmt: skip

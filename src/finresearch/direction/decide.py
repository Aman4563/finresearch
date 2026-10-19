"""The decision layer of the direction model (#327, specs.DECISION): CALL / PUT / spread / NO TRADE by expected value.

1. Probabilities → a distribution of the index at the horizon. The three event probabilities fix the masses of four
   return regions, R < −Kσ (down_k), −Kσ..0, 0..Kσ and R > Kσ (up_k), with σ = IV30·√(h/252). Inside each region the
   shape is the market-implied lognormal's (ln S[t+h]/S[t] ~ N((r − σ²/2)τ, σ²τ)), reweighted so the region's mass is
   the model's. Under the implied probabilities this is the implied lognormal itself. Incoherent inputs (P(up_k) >
   P(up), or P(up) + P(down_k) > 1) are clipped and the clipping is reported.
2. Each candidate trade (specs.DECISION.candidates, strikes from the live chain like the /fno presets) is valued at
   the horizon: Black-Scholes at each leg's own IV for the time left to expiry (sticky strike), intrinsic value when the
   expiry is the horizon. P&L = Σ qty·(value − entry price). Costs: entry charges, plus square-off charges at the entry
   prices when the trade is closed before expiry, or STT on long options that finish in the money when held to expiry
   (fincalc.charges). EV = Σ mass × P&L − costs, under the model's masses and under the implied ones.
3. A trade is recommended only when the probabilities come from an ACCEPTED cell, EV_model > EV_implied, and
   EV_model ≥ max(margin_frac_max_loss × max loss, min_ev_over_costs × costs). Otherwise "NO TRADE: no measured edge".
Nothing here places an order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from finresearch.direction import specs
from finresearch.fincalc import charges as ch
from finresearch.fincalc import options as o

LABELS = {"long_call": "Long call (ATM)", "long_put": "Long put (ATM)", "bull_call": "Bull call spread",
          "bear_put": "Bear put spread", "bull_put_credit": "Bull put credit spread",
          "bear_call_credit": "Bear call credit spread"}  # fmt: skip
VIEW = {"long_call": "up", "bull_call": "up", "bull_put_credit": "up", "long_put": "down", "bear_put": "down",
        "bear_call_credit": "down"}  # fmt: skip
GRID = 1200


@dataclass(frozen=True)
class CLeg:
    right: str  # call | put
    strike: float
    side: str  # buy | sell
    price: float  # entry premium per unit
    iv: float  # annual fraction


def region_masses(p: dict[str, float]) -> tuple[list[float], list[str]]:
    """[down_k, −Kσ..0, 0..Kσ, up_k] masses from P(up), P(up_k), P(down_k), with notes on any clipping."""
    notes = []
    up = min(1.0, max(0.0, p["up"]))
    upk = min(1.0, max(0.0, p["up_k"]))
    dk = min(1.0, max(0.0, p["down_k"]))
    if upk > up:
        notes.append(f"P(up more than Kσ) {upk:.3f} exceeded P(up) {up:.3f}: clipped to it")
        upk = up
    if dk > 1 - up:
        notes.append(f"P(down more than Kσ) {dk:.3f} exceeded P(not up) {1 - up:.3f}: clipped to it")
        dk = 1 - up
    return [dk, 1 - up - dk, up - upk, upk], notes


def horizon_grid(spot: float, iv_30: float, h: int, masses: list[float] | None,
                 steps: int = GRID) -> list[tuple[float, float]]:  # fmt: skip
    """(index level at the horizon, probability) on a ±6 sd grid of the implied lognormal, reweighted region by
    region to `masses` (None = the implied distribution itself)."""
    s = iv_30 / 100
    tau = h / specs.YEAR_SESSIONS
    sd = s * math.sqrt(tau)
    mu = (specs.RATE - s * s / 2) * tau
    k = specs.K * sd
    pts = []
    for i in range(steps):
        z = -6 + 12 * (i + 0.5) / steps
        r = mu + sd * z
        reg = 0 if r < -k else 1 if r <= 0 else 2 if r <= k else 3
        pts.append((spot * math.exp(r), math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi) * 12 / steps, reg))
    tot = [0.0] * 4
    for _, w, reg in pts:
        tot[reg] += w
    if masses is None:
        scale = [1 / sum(tot)] * 4
    else:
        scale = [masses[j] / tot[j] if tot[j] > 0 else 0.0 for j in range(4)]
    return [(x, w * scale[reg]) for x, w, reg in pts]


def leg_value(leg: CLeg, s: float, t_rem: float, rate: float) -> float:
    if t_rem <= 0:
        return max(0.0, s - leg.strike) if leg.right == "call" else max(0.0, leg.strike - s)
    return o.bs_price(leg.right, s, leg.strike, t_rem, rate, leg.iv)  # type: ignore[arg-type]


def evaluate_trade(legs: list[CLeg], qty: int, spot: float, grid_model: list[tuple[float, float]],
                   grid_implied: list[tuple[float, float]], t_rem: float, today: date, rate: float,
                   brokerage: float) -> dict[str, Any]:  # fmt: skip
    """EV after costs of one set (each leg `qty` units, signed by side) under both distributions (the two grids share
    their price points; only the masses differ)."""
    sign = {"buy": 1, "sell": -1}
    orders = [ch.TradeLeg("option", x.side, x.price, qty) for x in legs]  # type: ignore[arg-type]
    entry = ch.order_costs(orders, today, brokerage).total
    square = ch.order_costs(ch.flip(orders), today, brokerage).total if t_rem > 0 else 0.0
    stt_ex = float(ch.rates_as_of(today)["stt_option_exercise"].rate)

    def pnl(s: float) -> float:
        v = sum(sign[x.side] * qty * (leg_value(x, s, t_rem, rate) - x.price) for x in legs)
        if t_rem <= 0:  # held to expiry: STT on the intrinsic value of long options exercised in the money
            v -= stt_ex * sum(qty * leg_value(x, s, 0, rate) for x in legs if x.side == "buy")
        return v

    values = [pnl(s) for s, _ in grid_implied]

    def ev(grid: list[tuple[float, float]]) -> tuple[float, float]:
        e = sum(w * v for (_, w), v in zip(grid, values, strict=True))
        pop = sum(w for (_, w), v in zip(grid, values, strict=True) if v - entry - square > 0)
        return e - entry - square, pop

    ev_m, pop_m = ev(grid_model)
    ev_i, pop_i = ev(grid_implied)
    prof = o.profile([o.Leg(x.right, x.strike, x.price, sign[x.side] * qty) for x in legs], spot)  # type: ignore[arg-type]
    return {"ev_model": round(ev_m, 2), "ev_implied": round(ev_i, 2), "edge": round(ev_m - ev_i, 2),
            "pop_model": round(pop_m, 4), "pop_implied": round(pop_i, 4), "costs": round(entry + square, 2),
            "entry_costs": round(entry, 2), "exit_costs": round(square, 2),
            "max_loss": None if prof.max_loss is None else round(prof.max_loss, 2),
            "max_profit": None if prof.max_profit is None else round(prof.max_profit, 2),
            "net_premium": round(prof.net_premium, 2)}  # fmt: skip


def gate(row: dict[str, Any], accepted: bool, d: specs.Decision = specs.DECISION) -> tuple[bool, list[str]]:
    """Whether the trade clears the pre-registered gate, and why not."""
    why = []
    if not accepted:
        why.append("the probabilities come from no accepted model (market-implied, no edge)")
    if row["ev_model"] <= row["ev_implied"]:
        why.append("EV under the model is not above EV under the market's own probabilities")
    loss = abs(row["max_loss"]) if row["max_loss"] is not None else float("inf")
    need = max(d.margin_frac_max_loss * loss, d.min_ev_over_costs * row["costs"])
    if not row["ev_model"] >= need:
        why.append(f"EV after costs ₹{row['ev_model']:,.0f} is below the margin ₹{need:,.0f} "
                   f"(max of {d.margin_frac_max_loss:.0%} of the max loss and {d.min_ev_over_costs:g}× the costs)")  # fmt: skip
    return not why, why


def candidate_legs(chain, key: str) -> list[tuple[str, float, str]] | None:
    """(right, strike, side) for a candidate on `chain`, from the /fno presets (signals.fno.preset_legs): ATM legs, wings
    and credit-spread shorts about DECISION.width_pct of spot away."""
    from finresearch.signals import fno as fs

    preset = {"long_call": "long_call", "long_put": "long_put", "bull_call": "bull_call", "bear_put": "bear_put",
              "bull_put_credit": "iron_condor", "bear_call_credit": "iron_condor"}[key]  # fmt: skip
    legs = fs.preset_legs(chain, preset)
    if not legs:
        return None
    if key == "bull_put_credit":
        legs = [x for x in legs if x.right == "put"]
    elif key == "bear_call_credit":
        legs = [x for x in legs if x.right == "call"]
    return [(x.right, x.strike, x.side) for x in legs]


def _quote(chain, right: str, strike: float):
    row = next((r for r in chain.rows if float(r.strike) == strike), None)
    return None if row is None else (row.call if right == "call" else row.put)


def price_legs(chain, spec: list[tuple[str, float, str]], t_years: float, rate: float) -> list[CLeg] | str:
    spot = float(chain.underlying)
    out = []
    for right, k, side in spec:
        q = _quote(chain, right, k)
        px = float(q.last_price) if q is not None and q.last_price else None
        if px is None or px <= 0:
            return f"no traded price for the {right} {k:g}"
        iv = float(q.iv) / 100 if q.iv else o.implied_vol(right, px, spot, k, t_years, rate)  # type: ignore[arg-type]
        if not iv:
            return f"no implied volatility for the {right} {k:g}"
        out.append(CLeg(right, k, side, px, iv))
    return out


def decide(chain, lot: int, *, today: date, horizon_day: date, h: int, iv_30: float, probs_model: dict[str, float],
           accepted: bool, rate: float, brokerage: float) -> dict[str, Any]:  # fmt: skip
    """Every candidate's EV under the model and under the market, and the decision (module docstring)."""
    t_entry = max((chain.expiry - today).days, 0.5) / 365
    t_rem = max(0, (chain.expiry - horizon_day).days) / 365
    masses, notes = region_masses(probs_model)
    spot = float(chain.underlying)
    gm = horizon_grid(spot, iv_30, h, masses)
    gi = horizon_grid(spot, iv_30, h, None)
    rows = []
    for key in specs.DECISION.candidates:
        spec = candidate_legs(chain, key)
        if spec is None:
            rows.append({"key": key, "label": LABELS[key], "view": VIEW[key], "status": "unavailable",
                         "why": ["no traded strikes for this structure"]})  # fmt: skip
            continue
        legs = price_legs(chain, spec, t_entry, rate)
        if isinstance(legs, str):
            rows.append(
                {"key": key, "label": LABELS[key], "view": VIEW[key], "status": "unavailable", "why": [legs]}
            )
            continue
        r = evaluate_trade(legs, lot, spot, gm, gi, t_rem, today, rate, brokerage)
        ok, why = gate(r, accepted)
        rows.append({"key": key, "label": LABELS[key], "view": VIEW[key], "status": "passes" if ok else "fails",
                     "why": why, "legs": [{"right": x.right, "strike": x.strike, "side": x.side, "price": x.price,
                                           "iv": round(x.iv * 100, 2)} for x in legs], **r})  # fmt: skip
    passing = [r for r in rows if r["status"] == "passes"]
    best = max(passing, key=lambda r: r["ev_model"]) if passing else None
    ranked = sorted((r for r in rows if r.get("ev_model") is not None), key=lambda r: -r["ev_model"])
    return {
        "action": "TRADE" if best else "NO TRADE",
        "label": (f"{best['label']}: EV after costs ₹{best['ev_model']:,.0f} a set (market-implied ₹{best['ev_implied']:,.0f})"
                  if best else "NO TRADE: no measured edge"),
        "chosen": best, "candidates": rows, "best_by_model_ev": ranked[0]["key"] if ranked else None,
        "horizon": h, "horizon_day": horizon_day.isoformat(), "expiry": chain.expiry.isoformat(),
        "held_to_expiry": t_rem <= 0, "lot_size": lot, "spot": spot, "probabilities_used": probs_model,
        "accepted": accepted, "region_masses": [round(m, 4) for m in masses], "notes": notes,
        "method": ("EV = Σ probability × P&L at the horizon − costs; the model's region masses reweight the implied "
                   "lognormal (direction.decide). Entry at last traded prices."),
    }  # fmt: skip

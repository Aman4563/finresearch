"""The user's Groww F&O positions on /fno (#308): each contract resolved through Groww's instruments file, priced with
Groww's LTP (segment FNO), with greeks from fincalc.options at the option chain's IV, days to expiry and, per
underlying + expiry, the maximum loss at expiry when it is bounded.

What is unknown stays unknown: a row whose long/short direction is not confirmed (see connectors.groww) has no P&L,
greeks or max loss; a contract the instruments file does not list has no expiry or strike; a missing LTP is no P&L
(never 0); a group with an unknown leg has no max loss."""

from __future__ import annotations

from datetime import date
from typing import Any

from finresearch.fincalc import options as o

RIGHTS = {"CE": "call", "PE": "put", "FUT": "future"}


def contract(row: dict[str, str] | None) -> dict[str, Any] | None:
    """(underlying, expiry, strike, right, lot size) from an instruments-file FNO row; None when it is not usable."""
    if not row:
        return None
    right = RIGHTS.get((row.get("instrument_type") or "").strip().upper())
    try:
        expiry = date.fromisoformat((row.get("expiry_date") or "").strip()[:10])
    except ValueError:
        return None
    if right is None:
        return None
    try:
        strike = float(row.get("strike_price") or 0) if right != "future" else None
    except ValueError:
        return None
    try:
        lot = int(float(row.get("lot_size") or 0)) or None
    except ValueError:
        lot = None
    return {"underlying": (row.get("underlying_symbol") or "").strip().upper(), "expiry": expiry, "strike": strike,
            "right": right, "lot_size": lot}  # fmt: skip


def leg_greeks(right: str, strike: float, spot: float | None, t: float, rate: float,
               iv: float | None) -> o.Greeks | None:  # fmt: skip
    """Per-unit greeks; a future has delta 1 and nothing else; None without spot or IV."""
    if right == "future":
        return o.Greeks(spot or 0.0, 1.0, 0.0, 0.0, 0.0, 0.0) if spot else None
    if not spot or not iv or iv <= 0:
        return None
    try:
        return o.greeks(right, spot, strike, t, rate, iv)  # type: ignore[arg-type]
    except ValueError:
        return None


def chain_iv(chain: Any, right: str, strike: float) -> float | None:
    """The chain's IV for a strike as a fraction (chains carry % a year), None when absent."""
    if chain is None:
        return None
    for r in chain.rows:
        if float(r.strike) == strike:
            q = r.call if right == "call" else r.put
            return float(q.iv) / 100 if q is not None and q.iv else None
    return None


def implied(
    right: str, price: float | None, spot: float | None, strike: float, t: float, rate: float
) -> float | None:
    if not price or not spot or right == "future":
        return None
    return o.implied_vol(right, price, spot, strike, t, rate)  # type: ignore[arg-type]


def group_key(underlying: str, expiry: date) -> str:
    return f"{underlying}:{expiry.isoformat()}"


def max_loss(legs: list[dict[str, Any]], spot: float | None) -> tuple[float | None, str]:
    """The group's loss at expiry (₹, negative) when bounded, from fincalc.options.profile with the positions' average
    prices as premiums, with a note when it is bounded only by a price of zero (#351). (None, reason) when unbounded or
    not computable."""
    if not legs or spot is None:
        return None, "no underlying price"
    if any(not x["direction_known"] or x.get("right") is None or (x["right"] != "future" and x.get("strike") is None)
           for x in legs):  # fmt: skip
        return None, "a leg's direction or contract is unknown"
    if any(x["avg_price"] is None for x in legs):
        return None, "a leg has no average price"
    ol = [o.Leg(x["right"], x["avg_price"] if x["right"] == "future" else x["strike"],
                0.0 if x["right"] == "future" else x["avg_price"], int(x["qty"])) for x in legs]  # fmt: skip
    prof = o.profile(ol, spot)
    if prof.max_loss is None:
        return None, "unbounded (a naked short call or a short future)"
    # #351: the same two-slope rule as the F&O gate: a loss that stops only at a price of zero is not defined risk
    return prof.max_loss, "" if prof.defined_risk else "bounded only by a price of zero: not defined risk"

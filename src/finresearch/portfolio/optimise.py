"""Holding-level portfolio optimiser with a tax-aware trade list (#346, first slice). Informational only.

Illustrative, not investment advice (the app is not a SEBI-registered investment adviser or research analyst; SEBI
(Investment Advisers) Regulations 2013). Brokers are read-only: the output is a list of suggested trades, never an
order, and nothing here can place one.

1. Universe: today's open holdings with a value. A holding without any price is left out and the plan says it is
   incomplete. A holding that cannot be traded today (no live price, a Sovereign Gold Bond) or whose daily returns
   do not cover the validation span (`NEED` = one training year plus `MIN_WINDOWS` test months) is *fixed* at its
   current weight with the reason: never dropped, which would quietly re-weight everything else.
2. Bounds per optimised holding: lower = the units that cannot be sold today (rebalance.sellable: ELSS lock-in,
   exit-load period, a lot with an unknown cost or date; FIFO, CBDT Circular 768) at today's price; upper = the
   profile's single-stock position limit (portfolio.limits.position_limit) for stocks, none for funds. Bounds that
   cannot add up to 100 % are reported as infeasible, never relaxed.
3. Weights: optimise_math (Ledoit-Wolf covariance of the last `TRAIN` aligned daily returns; minimum variance, risk
   parity or HRP; no expected returns). Validation first: the pre-registered walk-forward check (optimise_math.TRAIN,
   TEST, MIN_WINDOWS, MAX_TURNOVER) must pass, else the comparison is shown with no targets and no trades.
4. Trades: a holding moves only when its weight changes by at least `MIN_TRADE_PP`. Sales take the sellable FIFO
   prefix; tax per step is the change in this financial year's tax incl. cess against the gains already realised
   this year and the earlier steps (rebalance._steps, i.e. fincalc.tax.tax_delta, #413's engine). Buys are funded by
   the net proceeds (scaled down pro rata if short). Charges: STT and stamp duty as rebalance; brokerage, DP and
   exchange charges are unknown (flagged on every trade), a fund sale with no exit load entered has an unknown exit
   load (flagged, never ₹0), a step whose tax cannot be computed has tax None and so has the total.
   Costs are counted, not optimised, in this slice (a cost penalty in the objective is the next one).
Survivorship: the universe is the holdings you own today, so the validation never sees holdings you sold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Any

import numpy as np

from finresearch.fincalc.dates import fiscal_year
from finresearch.portfolio import optimise_math as om
from finresearch.portfolio.rebalance import (
    EPS,
    Piece,
    Position,
    _row,
    _step,
    _steps,
    buy_stamp,
    sellable,
)
from finresearch.portfolio.tax import DisposalRow, gains_of, rules_note, unclassified

ZERO = Decimal(0)
NEED = om.TRAIN + om.TEST * om.MIN_WINDOWS  # aligned daily returns the validation needs
MAX_RETURNS = 5 * 252  # the most recent five years are used
MIN_TRADE_PP = 1.0  # a holding moves only when its weight changes by at least 1 percentage point
UNKNOWN_BROKERAGE = "brokerage, DP and exchange charges (not known to the app)"
UNKNOWN_EXIT_LOAD = "exit load (none entered for this fund under Costs; check the scheme document)"
DISCLAIMER = ("Illustrative, not investment advice: suggested trades only, no order is placed or can be placed from "
              "the app. FinResearch is not a SEBI-registered investment adviser or research analyst. Check the tax "
              "with a CA before you act.")  # fmt: skip


def _f(x: Decimal | float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def _category(p: Position, lot: Any, units: Decimal, today: date) -> int:
    """rebalance.CATEGORIES label of a slice: 0/1 short/long-term loss, 3/4 long/short-term gain."""
    r = _row(p, lot, units, today, (p.price or ZERO) * units)
    long = r.cls is not None and r.cls.term == "long"
    if (r.gain or ZERO) < 0:
        return 1 if long else 0
    return 3 if long else 4


def _aligned(ids: list[int], returns: Mapping[int, Mapping[date, float]]) -> tuple[list[int], list[date]]:
    """Greedy: the holdings with the longest histories first, each kept while the common days stay >= NEED."""
    keep: list[int] = []
    common: set[date] | None = None
    for hid in sorted(ids, key=lambda h: -len(returns.get(h) or {})):
        ds = set(returns.get(hid) or {})
        both = ds if common is None else common & ds
        if len(both) >= NEED:
            keep, common = [*keep, hid], both
    return keep, sorted(common or [])[-MAX_RETURNS:]


def _units(amount: Decimal, p: Position, rounding: str) -> Decimal:
    assert p.price is not None
    st = _step(p)
    return (amount / p.price / st).to_integral_value(rounding=rounding) * st


def _sell_pieces(p: Position, units: Decimal, lots: list[Any], today: date) -> list[Piece]:
    out, left = [], units
    for lot in lots:
        if left <= EPS:
            break
        take = min(left, lot.quantity)
        out.append(Piece(p, lot, take, _category(p, lot, take, today)))
        left -= take
    return out


def _flag_sells(steps: list[dict[str, Any]], by_id: Mapping[int, Position]) -> None:
    for s in steps:
        p = by_id[s["holding_id"]]
        s["costs_unknown"] = []
        if p.asset_type != "mf":
            s["costs_unknown"].append(UNKNOWN_BROKERAGE)
        elif p.exit_load is None:
            s["exit_load"] = None  # unknown, never ₹0
            s["costs_unknown"].append(UNKNOWN_EXIT_LOAD)
            s["notes"] = [n for n in s["notes"] if "assumed none" not in n]
        if s["tax"] is None:
            s["notes"].append("tax unknown: a lot's term could not be determined")


def suggest(positions: Sequence[Position], returns: Mapping[int, Mapping[date, float]], today: date,
            realised: Sequence[DisposalRow], slab: Decimal, *, method: str = "min_variance",
            max_position_pct: float | None = None) -> dict[str, Any]:  # fmt: skip
    """The optimiser's payload. `returns`: holding id -> daily returns (history.returns_of); `realised`: disposals
    already booked (this FY's are the tax base); `max_position_pct`: the single-stock limit (None: no limit)."""
    if method not in om.METHODS:
        raise ValueError(f"unknown method {method!r}: one of {', '.join(om.METHODS)}")
    fy = fiscal_year(today)
    base_rows = [r for r in realised if r.fy == fy]
    unk = unclassified(base_rows)
    excluded = [{"holding_id": p.holding.id, "name": p.holding.name, "reason": p.price_note or "no price"}
                for p in positions if p.value is None and p.lots]  # fmt: skip
    priced = [p for p in positions if p.value is not None and p.value > 0 and p.lots]
    out: dict[str, Any] = {
        "method": method, "methods": list(om.METHODS), "as_of": today.isoformat(), "disclaimer": DISCLAIMER,
        "complete": not excluded, "excluded": excluded, "tax_complete": unk["count"] == 0, "unclassified": unk,
        **rules_note([fy]), "min_trade_pp": MIN_TRADE_PP, "need_returns": NEED,
        "max_position_pct": max_position_pct, "holdings": [], "skipped": [], "sells": [], "buys": [],
        "validation": None, "risk": None, "totals": None, "notes": list(NOTES),
    }  # fmt: skip
    if not priced:
        return {**out, "status": "no_value", "message": "Nothing is valued yet: no holding has a price."}
    total = sum((p.value or ZERO for p in priced), ZERO)
    w0 = {p.holding.id: float((p.value or ZERO) / total) for p in priced}
    fixed: dict[int, str] = {}
    cand = []
    for p in priced:
        n = len(returns.get(p.holding.id) or {})
        if p.asset_class == "Sovereign Gold Bonds" or p.holding.tax_class == "sgb":
            fixed[p.holding.id] = (
                "Sovereign Gold Bond: a market sale gives up the tax-free redemption at maturity"
            )
        elif p.price is None:
            fixed[p.holding.id] = p.price_note or "no live price today"
        elif n < NEED:
            fixed[p.holding.id] = f"only {n} daily returns (the validation needs {NEED})"
        else:
            cand.append(p.holding.id)
    keep, ds = _aligned(cand, returns)
    for hid in set(cand) - set(keep):
        fixed[hid] = f"its price history shares fewer than {NEED} days with the other holdings'"
    by_id = {p.holding.id: p for p in priced}
    budget = sum(w0[h] for h in keep)
    cap = None if max_position_pct is None else max_position_pct / 100
    sl = {h: sellable(by_id[h], today) for h in keep}
    lo = np.array([float(sl[h].blocked_units * (by_id[h].price or ZERO) / total) for h in keep])
    hi = np.array([cap if cap is not None and by_id[h].asset_type == "stock" else 1.0 for h in keep])
    hi = np.maximum(hi, lo)
    out["skipped"] = [{"holding_id": h, "name": by_id[h].holding.name, "units": _f(sl[h].blocked_units, 4),
                       "reason": sl[h].reason} for h in keep if sl[h].blocked_units > EPS]  # fmt: skip

    def rows(target: dict[int, float] | None) -> list[dict[str, Any]]:
        b = dict(zip(keep, zip(lo, hi, strict=True), strict=True))
        return [{"holding_id": p.holding.id, "name": p.holding.name, "asset_class": p.asset_class,
                 "current_pct": round(w0[p.holding.id] * 100, 2),
                 "target_pct": (round(target[p.holding.id] * 100, 2) if target is not None else None),
                 "lo_pct": round(b[p.holding.id][0] * 100, 2) if p.holding.id in b else None,
                 "hi_pct": round(min(b[p.holding.id][1], 1.0) * 100, 2) if p.holding.id in b else None,
                 "status": "optimised" if p.holding.id in b else "fixed", "reason": fixed.get(p.holding.id)}
                for p in priced]  # fmt: skip

    out["holdings"] = rows(None)
    if len(keep) < 2:
        return {**out, "status": "too_few", "message": f"Fewer than two holdings have {NEED} days of returns in "
                "common and can be traded today: nothing to optimise."}  # fmt: skip
    lo_b, hi_b = lo / budget, np.minimum(hi / budget, 1.0)
    try:
        om.check_bounds(lo_b, hi_b)
    except om.Infeasible as e:
        return {**out, "status": "infeasible", "message": f"No weights fit the limits: {e}."}
    r = np.array([[returns[h][d] for h in keep] for d in ds])
    cur = np.array([w0[h] for h in keep]) / budget
    v = om.validate(r, method, lo_b, hi_b, cur)
    out["validation"] = v
    if not v["passed"]:
        return {**out, "status": "not_validated", "message": "The method did not pass the out-of-sample check, so "
                "no target or trade is suggested: " + "; ".join(v["reasons"]) + "."}  # fmt: skip
    cov, delta = om.ledoit_wolf(r[-om.TRAIN :])
    w = om.optimise(method, cov, lo_b, hi_b)
    target = {**{h: w0[h] for h in fixed}, **{h: float(w[i] * budget) for i, h in enumerate(keep)}}
    out["holdings"] = rows(target)
    out["risk"] = {"shrinkage": round(delta, 4), "days": len(ds), "window": om.TRAIN,
                   "vol_current": round(float(np.sqrt(cur @ cov @ cur * om.TRADING_DAYS)), 4),
                   "vol_target": round(float(np.sqrt(w @ cov @ w * om.TRADING_DAYS)), 4),
                   "note": "ex-ante annualised volatility of the optimised holdings (Ledoit-Wolf, last year)"}  # fmt: skip
    return {**out, **_trades(by_id, keep, sl, w0, target, total, today, base_rows, slab)}


def _trades(by_id: Mapping[int, Position], keep: list[int], sl: Mapping[int, Any], w0: Mapping[int, float],
            target: Mapping[int, float], total: Decimal, today: date, base_rows: Sequence[DisposalRow],
            slab: Decimal) -> dict[str, Any]:  # fmt: skip
    fy = fiscal_year(today)
    move = {h: target[h] - w0[h] for h in keep if abs(target[h] - w0[h]) * 100 >= MIN_TRADE_PP}
    pieces: list[Piece] = []
    for h in sorted((h for h in move if move[h] < 0), key=lambda h: h):
        p = by_id[h]
        can = sum((x.quantity for x in sl[h].lots), ZERO)
        units = min(can, _units(Decimal(str(-move[h])) * total, p, "ROUND_HALF_UP"))
        pieces += _sell_pieces(p, units, sl[h].lots, today) if units > 0 else []
    pieces.sort(
        key=lambda x: (x.category, x.pos.holding.id)
    )  # losses first, so later gains are taxed after them
    steps, _ = _steps(pieces, today, gains_of(base_rows), fy, slab)
    _flag_sells(steps, by_id)
    net = sum((Decimal(str(s["net"])) for s in steps), ZERO)
    want = {h: Decimal(str(m)) * total for h, m in move.items() if m > 0}
    scale = min(Decimal(1), net / sum(want.values(), ZERO)) if want else ZERO
    buys, unfunded = [], ZERO
    for h, amt in sorted(want.items(), key=lambda kv: -kv[1]):
        p = by_id[h]
        assert p.price is not None
        units = _units(amt * scale, p, ROUND_DOWN)
        spend = units * p.price
        unfunded += amt - spend
        if units > 0:
            buys.append({"holding_id": h, "name": p.holding.name, "asset_class": p.asset_class,
                         "units": _f(units, 4), "price": _f(p.price, 4), "amount": _f(spend),
                         "stamp": _f(buy_stamp(p.asset_class, spend)),
                         "costs_unknown": [UNKNOWN_BROKERAGE] if p.asset_type != "mf" else []})  # fmt: skip
    taxes = [s["tax"] for s in steps]
    unknown = sorted({c for x in steps + buys for c in x["costs_unknown"]})
    totals = {"sold_gross": _f(sum((Decimal(str(s["gross"])) for s in steps), ZERO)),
              "sell_charges": _f(sum((Decimal(str(s["charges"])) for s in steps), ZERO)),
              "stamp_duty": _f(sum((Decimal(str(b["stamp"])) for b in buys), ZERO)),
              "tax": None if any(t is None for t in taxes) else _f(sum((Decimal(str(t)) for t in taxes), ZERO)),
              "bought": _f(sum((Decimal(str(b["amount"])) for b in buys), ZERO)), "unfunded": _f(unfunded),
              "costs_unknown": unknown}  # fmt: skip
    if not steps and not buys:
        status, msg = (
            "suggested",
            f"Every optimised holding is within {MIN_TRADE_PP:g} pp of its target: no trade.",
        )
    else:
        status, msg = (
            "suggested",
            "Suggested trades toward the validated target (informational; nothing is placed).",
        )
    return {"status": status, "message": msg, "sells": steps, "buys": buys, "totals": totals}


NOTES = [
    "Methods use no expected returns: minimum variance, risk parity or hierarchical risk parity on a Ledoit-Wolf "
    "covariance of the last year of daily returns.",
    "Out-of-sample check (pre-registered): monthly refits on the year before each month, held with drift; the "
    "method must have no higher realised volatility than equal weight and your current weights over at least six "
    "months, with turnover under 100 % a year. Past volatility does not guarantee future volatility.",
    "Only the holdings you own today are in the check (survivorship): holdings you sold are not.",
    "Sales are first-in-first-out (CBDT Circular 768). Units in an ELSS lock-in or an exit-load period and lots with "
    "an unknown cost or date are never sold: they set each holding's lower bound.",
    "Tax per sale is the change in this financial year's capital-gains tax incl. 4 % cess after the gains already "
    "realised this year (fincalc.tax); brought-forward losses and surcharge are not applied here.",
    "Charges: STT and stamp duty only. Brokerage, DP and exchange charges are unknown and flagged on each trade; "
    "a fund sale without an exit load entered is flagged too. Costs are counted, not optimised.",
    "Buys are paid from the net sale proceeds; tax is paid later from your own money.",
]


# --------------------------------------------------------------------------- price history
HISTORY_YEARS = 4  # closes read per holding: enough for NEED (about 2.5 years) with gaps to spare


async def load_returns(hist: Any, fetch: Any, store: Any, today: date) -> dict[int, dict[date, float]]:
    """Holding id -> daily unit-adjusted returns (history.returns_of) of its instrument over the last
    `HISTORY_YEARS`, read through the analytics PriceStore (as factor_exposures.load). A failed read is an empty
    history, so the holding is fixed with its reason; it never becomes a 0 return."""
    from datetime import timedelta

    from finresearch.fincalc.dates import add_years
    from finresearch.portfolio.history import Instrument, returns_of

    start = add_years(today, -HISTORY_YEARS) - timedelta(days=10)
    out: dict[int, dict[date, float]] = {}
    for p in hist.positions:
        if p.value <= 0:
            continue
        kind, ident = p.key.split(":", 1)
        inst = Instrument(p.key, "mf" if kind == "MF" else kind.lower(), ident, p.scheme)
        try:
            closes = await store.get(p.key, start, today, lambda a, b, i=inst: fetch(i, a, b), today)
        except Exception:
            closes = {}
        r = returns_of(dict(closes), dict(hist.factors.get(p.key) or {})) if len(closes) > 1 else {}
        for hid in p.holding_ids:
            out[hid] = r
    return out

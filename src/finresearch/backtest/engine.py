"""The event loop (#307): every session's close, oldest first, for each underlying: mark and manage the open positions,
then look for an entry. Everything a decision reads comes through `history.Clocked`, which raises on any later day.

Order at each close: (1) positions entered on an earlier session are marked at the reference prices and closed when a
management rule fires (profit-take, stop, days-to-expiry, the last session before expiry day) or settled on expiry
day (hold-to-expiry specs); (2) an entry is considered. A position is never managed on its entry day.
Costs: fincalc.charges on every executed order (entry and square-off; brokerage per order), and STT on long options
that settle in the money (fincalc.paper.settle). Sizing: lots = floor(Book.max_loss_pct % of capital ÷ (defined max
loss + entry costs) per lot), capped by Book.max_lots and by the approximate margin (fincalc.margin_approx) of all open
positions staying within the capital. Overlays trade one lot. Equity = capital + closed net P&L + open positions'
marked gross P&L − their entry costs, recorded at every session from the window's start.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from finresearch.backtest import pricing as px
from finresearch.backtest.history import Clocked, Day, History, Quote
from finresearch.backtest.specs import BOOK, Book, Spec
from finresearch.fincalc import charges as ch
from finresearch.fincalc import margin_approx as ma
from finresearch.fincalc import paper
from finresearch.fincalc import volatility as vol

INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


@dataclass
class Leg:
    right: str
    strike: float
    side: str  # buy | sell
    qty: int  # units
    entry: float  # fill
    iv: float | None  # Black-76 IV at entry (fraction)
    mark: float  # last reference price
    exit: float | None = None

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1


@dataclass
class Position:
    spec: str
    symbol: str
    expiry: date
    entry_day: date
    dte: int
    lots: int
    lot: int
    legs: list[Leg]
    premium: float  # ₹ received (+, credit) or paid (−, debit) at the fills
    max_loss: float | None  # ₹ defined maximum loss before costs (None: an overlay's open-ended leg)
    margin: float | None  # ₹ approximate margin at entry (None: unknown)
    entry_costs: float
    iv30: float | None
    iv_pct: float | None
    stock_entry: float | None = None  # overlays: the same-expiry future at entry (the held stock's proxy)
    flags: list[str] = field(default_factory=list)

    def gross(self) -> float:
        return sum(x.sign * (x.mark - x.entry) * x.qty for x in self.legs)


def _trade(p: Position, d: date, reason: str, gross: float, exit_costs: float, stt: float,
           stock_exit: float | None) -> dict[str, Any]:  # fmt: skip
    net = gross - p.entry_costs - exit_costs - stt
    units = p.lots * p.lot
    return {
        "spec": p.spec, "symbol": p.symbol, "entry_day": p.entry_day.isoformat(), "exit_day": d.isoformat(),
        "expiry": p.expiry.isoformat(), "dte": p.dte, "days_held": (d - p.entry_day).days, "lots": p.lots,
        "lot": p.lot, "premium": round(p.premium, 2), "max_loss": None if p.max_loss is None else round(p.max_loss, 2),
        "margin": None if p.margin is None else round(p.margin, 2), "entry_costs": round(p.entry_costs, 2),
        "exit_costs": round(exit_costs, 2), "exercise_stt": round(stt, 2), "gross": round(gross, 2),
        "net": round(net, 2), "reason": reason, "iv30": p.iv30, "iv_pct": p.iv_pct, "flags": p.flags,
        "legs": [{"right": x.right, "strike": x.strike, "side": x.side, "qty": x.qty, "entry": x.entry,
                  "exit": x.exit, "iv": None if x.iv is None else round(x.iv, 4)} for x in p.legs],
        "stock_pnl": None if p.stock_entry is None or stock_exit is None
        else round((stock_exit - p.stock_entry) * units, 2),
    }  # fmt: skip


@dataclass
class SpecState:
    spec: Spec
    open: list[Position] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity: list[tuple[date, float]] = field(default_factory=list)
    realised: float = 0.0
    last_entry: date | None = None
    skips: Counter = field(default_factory=Counter)  # entries refused, by reason
    notes: Counter = field(default_factory=Counter)  # entries made with a caveat
    entries_considered: int = 0


# --------------------------------------------------------------------------- management
def mark(p: Position, x: Day) -> dict[int, Quote | None]:
    """Update each leg's mark to today's reference price; a leg with no row today keeps its last mark (flagged)."""
    out: dict[int, Quote | None] = {}
    for i, leg in enumerate(p.legs):
        q = x.options.get((p.expiry, leg.right, leg.strike))
        out[i] = q
        if q is not None and q.ref is not None:
            leg.mark = q.ref
        elif f"stale mark {x.day}" not in p.flags:
            p.flags.append(f"stale mark {x.day}")
    return out


def exit_reason(p: Position, spec: Spec, d: date, next_session: date | None) -> str | None:
    g = p.gross()
    basis = abs(p.premium)
    if spec.profit_take_pct is not None and basis > 0 and g >= spec.profit_take_pct / 100 * basis:
        return "profit_take"
    if spec.stop_mult is not None and basis > 0 and g <= -spec.stop_mult * basis:
        return "stop"
    if spec.exit_dte is not None and (p.expiry - d).days <= spec.exit_dte:
        return "dte"
    if not spec.hold_to_expiry and next_session is not None and next_session >= p.expiry:
        return "before_expiry_day"
    return None


def square_off(
    p: Position, x: Day, quotes: dict[int, Quote | None], reason: str, book: Book
) -> dict[str, Any]:
    """Close every leg at today's reference price moved by slippage; charges on the closing orders."""
    orders = []
    for i, leg in enumerate(p.legs):
        q = quotes.get(i)
        contracts = q.contracts if q is not None and q.ref is not None else 0  # a stale leg: the widest tier
        side = "sell" if leg.side == "buy" else "buy"
        leg.exit = px.fill(leg.mark, side, contracts, book)
        orders.append(ch.TradeLeg("option", side, leg.exit, leg.qty))  # type: ignore[arg-type]
    gross = sum(leg.sign * (leg.exit - leg.entry) * leg.qty for leg in p.legs)  # type: ignore[operator]
    costs = ch.order_costs(orders, x.day, book.brokerage_per_order).total
    return _trade(p, x.day, reason, gross, costs, 0.0, x.futures.get(p.expiry))


def settle(p: Position, x: Day) -> dict[str, Any] | None:
    """Expiry-day settlement at the final settlement price (pricing.settlement_price); STT on in-the-money longs."""
    got = px.settlement_price(x, p.expiry)
    if got is None:
        return None
    s, source = got
    legs = [paper.PaperLeg(leg.right, leg.strike, leg.side, leg.qty, leg.entry) for leg in p.legs]  # type: ignore[arg-type]
    st = paper.settle(legs, s, p.expiry)
    for leg, v in zip(p.legs, st.final_values, strict=True):
        leg.exit = v
    if source != "future":
        p.flags.append(f"settled at {source} {s:.2f}")
    return _trade(p, x.day, "expiry", st.gross, 0.0, st.exercise_stt, x.futures.get(p.expiry))


# --------------------------------------------------------------------------- entry
def choose_legs(spec: Spec, x: Day, expiry: date, fwd: float, book: Book) -> list[tuple[Quote, str]] | str:
    """The (quote, side) legs the spec's rule picks at this close, or the reason none can be picked."""
    r, a, m = book.rate, spec.anchor_delta, book.min_contracts_anchor
    w = (spec.width_pct or 0) / 100 * fwd

    def credit(right: str) -> list[tuple[Quote, str]] | str:
        short = px.by_delta(x, expiry, right, a, fwd, r, m)
        if short is None:
            return f"no traded {right} near {a:g} delta"
        far = px.wing(
            x, expiry, right, short.strike, short.strike - w if right == "put" else short.strike + w
        )
        return [(short, "sell"), (far, "buy")] if far else f"no traded {right} wing"

    def debit(right: str) -> list[tuple[Quote, str]] | str:
        near = px.by_delta(x, expiry, right, a, fwd, r, m, otm_only=False)
        if near is None:
            return f"no traded {right} near {a:g} delta"
        far = px.wing(x, expiry, right, near.strike, near.strike + w if right == "call" else near.strike - w)
        return [(near, "buy"), (far, "sell")] if far else f"no traded {right} wing"

    if spec.kind in ("put_credit", "call_credit"):
        return credit(spec.kind.split("_")[0])
    if spec.kind == "iron_condor":
        p, c = credit("put"), credit("call")
        return p if isinstance(p, str) else c if isinstance(c, str) else p + c
    if spec.kind in ("debit_call", "debit_put"):
        return debit(spec.kind.split("_")[1])
    one = px.by_delta(x, expiry, "call" if spec.kind == "covered_call" else "put", a, fwd, r, m)
    if one is None:
        return f"no traded option near {a:g} delta"
    return [(one, "sell" if spec.kind == "covered_call" else "buy")]


def max_loss_per_unit(spec: Spec, legs: list[tuple[Quote, str]], premium: float) -> float | None:
    """Defined maximum loss per unit at the fills (before costs); None for the covered call's short call."""
    if spec.kind == "covered_call":
        return None
    if spec.kind in ("debit_call", "debit_put", "protective_put"):
        return -premium
    widths = {}
    for right in ("put", "call"):
        ks = [q.strike for q, _ in legs if q.right == right]
        if len(ks) == 2:
            widths[right] = abs(ks[0] - ks[1])
    return max(widths.values()) - premium


def margin(spec: Spec, sym: str, legs: list[Leg], fwd: float, t: float, book: Book) -> float | None:
    """fincalc.margin_approx on the basket; None when a leg has no IV (unknown, never 0). A covered call's margin is
    the held stock (pledged), so it is None here and the overlay is judged on the stock's value instead."""
    if spec.kind == "covered_call" or any(x.iv is None for x in legs):
        return None
    ml = [ma.MarginLeg(x.right, x.strike, x.qty * x.sign, x.mark, x.iv) for x in legs]
    kind = "index" if sym in INDEXES else "stock"
    return ma.estimate(ml, px.discounted_spot(fwd, t, book.rate), t, book.rate, kind).total


def try_enter(st: SpecState, x: Day, sym: str, h: Clocked, ivs: list[float], book: Book) -> Position | None:
    """One entry decision at this close (module docstring); a refusal is counted in st.skips by reason."""
    spec, d = st.spec, x.day
    if len(st.open) >= spec.max_concurrent:
        return None
    if st.last_entry is not None and (d - st.last_entry).days < spec.min_days_between:
        return None
    want_monthly = spec.expiry_kind == "monthly"
    exps = [
        e
        for e in x.expiries()
        if spec.dte_min <= (e - d).days <= spec.dte_max and x.is_monthly(e) == want_monthly
    ]
    exps = [e for e in exps if sum(p.expiry == e for p in st.open) < spec.max_per_expiry]
    if not exps:
        return None
    st.entries_considered += 1
    expiry = exps[0]
    stats = vol.iv_stats(ivs) if x.iv30 is not None else None  # today's IV unknown -> the filter is unknown
    pct = stats.percentile if stats else None
    if spec.iv_pct_min is not None and (pct is None or pct < spec.iv_pct_min):
        st.skips["IV percentile unknown" if pct is None else "IV percentile below the filter"] += 1
        return None
    fwd = px.forward(x, expiry, book.rate)
    if fwd is None:
        st.skips["no forward"] += 1
        return None
    picked = choose_legs(spec, x, expiry, fwd, book)
    if isinstance(picked, str):
        st.skips[picked] += 1
        return None
    lot = h.lot(sym, expiry, d)
    if not lot:
        st.skips["lot size unknown"] += 1
        return None
    t = (expiry - d).days / 365
    fills = [(q, side, px.fill(q.ref, side, q.contracts, book)) for q, side in picked]  # type: ignore[arg-type]
    prem_unit = sum((f if side == "sell" else -f) for _, side, f in fills)
    if spec.kind in ("put_credit", "call_credit", "iron_condor") and prem_unit <= 0:
        st.skips["no credit after slippage"] += 1
        return None
    loss_unit = max_loss_per_unit(spec, picked, prem_unit)
    if loss_unit is not None and loss_unit <= 0:
        st.skips["no risk at these prices (data error?)"] += 1
        return None

    def build(lots: int) -> tuple[list[Leg], float, float | None]:
        legs = [Leg(q.right, q.strike, side, lots * lot, f, px.leg_iv(q, fwd, t, book.rate), q.ref)  # type: ignore[arg-type]
                for q, side, f in fills]  # fmt: skip
        costs = ch.order_costs([ch.TradeLeg("option", lg.side, lg.entry, lg.qty) for lg in legs], d,  # type: ignore[arg-type]
                               book.brokerage_per_order).total  # fmt: skip
        return legs, costs, margin(spec, sym, legs, fwd, t, book)

    if spec.role == "overlay":
        lots = 1
    else:
        one = build(1)
        lots = min(
            book.max_lots, math.floor(book.capital * book.max_loss_pct / 100 / (loss_unit * lot + one[1]))
        )  # type: ignore[operator]
    used = sum(p.margin or 0.0 for p in st.open)
    while lots >= 1:
        legs, costs, mg = build(lots)
        if mg is None or used + mg <= book.capital:
            break
        lots -= 1
    if lots < 1:
        st.skips["max loss or margin above the budget for one lot"] += 1
        return None
    if mg is None and spec.kind != "covered_call":
        st.notes["margin unknown (traded; excluded from return on margin)"] += 1
    p = Position(spec.key, sym, expiry, d, (expiry - d).days, lots, lot, legs, prem_unit * lots * lot,
                 None if loss_unit is None else loss_unit * lots * lot, mg, costs, x.iv30, pct,
                 x.futures.get(expiry) if spec.role == "overlay" else None)  # fmt: skip
    st.open.append(p)
    st.last_entry = d
    return p


# --------------------------------------------------------------------------- the loop
def manage(st: SpecState, x: Day, next_session: date | None, book: Book) -> None:
    for p in list(st.open):
        if p.entry_day >= x.day:
            continue
        tr = None
        if x.day >= p.expiry:
            tr = settle(p, x) if x.day == p.expiry else None
            if tr is None:  # no settlement price on expiry day, or expiry day missing from the data
                p.flags.append("no settlement price: closed at the day's prices")
                tr = square_off(p, x, mark(p, x), "expiry_unsettled", book)
        else:
            quotes = mark(p, x)
            reason = exit_reason(p, st.spec, x.day, next_session)
            if reason:
                tr = square_off(p, x, quotes, reason, book)
        if tr is not None:
            st.trades.append(tr)
            st.realised += tr["net"]
            st.open.remove(p)


def run(specs: list[Spec], history: History, start: date, end: date, book: Book = BOOK,
        warmup_days: int = 400, data_end: date | None = None) -> dict[str, dict[str, Any]]:  # fmt: skip
    """Replay `specs` (concrete underlyings) with entries from `start` to `end`; open positions are managed after
    `end` until they close or the data ends (`data_end`, default the latest stored day). Returns per spec key:
    trades, the daily equity curve, refusals and the positions still open at the end."""
    from datetime import timedelta

    h = Clocked(history)
    groups: dict[str, list[Spec]] = {}
    for s in specs:
        groups.setdefault(s.underlying, []).append(s)
    out: dict[str, dict[str, Any]] = {}
    for sym, group in groups.items():
        states = [SpecState(s) for s in group]
        last = data_end or history.latest(sym)
        days = h.sessions(sym, start - timedelta(days=warmup_days), last) if last else []
        ivs: list[float] = []
        for i, d in enumerate(days):
            h.advance(d)
            x = h.day(sym, d)
            if x is None:
                continue
            if x.iv30 is not None:
                ivs.append(x.iv30)
            if d < start:
                continue
            nxt = days[i + 1] if i + 1 < len(days) else None
            for st in states:
                manage(st, x, nxt, book)
                if d <= end:
                    try_enter(st, x, sym, h, ivs, book)
                if d <= end or st.open:
                    st.equity.append(
                        (d, book.capital + st.realised + sum(p.gross() - p.entry_costs for p in st.open))
                    )
        for st in states:
            out[st.spec.key] = {
                "spec": st.spec, "symbol": sym, "trades": st.trades, "equity": st.equity,
                "skips": dict(st.skips), "notes": dict(st.notes), "entries_considered": st.entries_considered,
                "open_at_end": [{"entry_day": p.entry_day.isoformat(), "expiry": p.expiry.isoformat(),
                                 "marked_gross": round(p.gross(), 2)} for p in st.open],
                "max_day_read": h.max_seen, "sessions": len(days),
            }  # fmt: skip
    return out


def resolve(spec: Spec, stock: str | None) -> Spec | None:
    """A STOCK_RULE spec on the chosen stock (None when no stock options are stored)."""
    if spec.underlying != "STOCK_RULE":
        return spec
    return replace(spec, underlying=stock) if stock else None

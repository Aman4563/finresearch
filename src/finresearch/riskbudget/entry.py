"""One entry decision of the registered engine (backtest.engine.try_enter), split at its sizing (#395 Stage 2): the
candidate (the spec's checks, legs and fills at this close, with the registered per-trade lots), the engine's margin
rule applied to a whole book's open positions, and the placed position. Every check, refusal reason and figure is the
engine's; only the lots between `candidate` and `place` can come from an arm's book-level limits. backtest/ is not
edited (its files are the vault's pinned engine); this module calls its public functions."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from finresearch.backtest import engine
from finresearch.backtest import pricing as px
from finresearch.backtest.history import Day, Quote
from finresearch.backtest.specs import Book, Spec
from finresearch.fincalc import charges as ch
from finresearch.fincalc import volatility as vol

Built = tuple[list[engine.Leg], float, float | None]  # legs, entry costs, margin (None = unknown)
CREDIT = ("put_credit", "call_credit", "iron_condor")


@dataclass
class Candidate:
    spec: Spec
    symbol: str
    day: date
    expiry: date
    lot: int
    picked: list[
        tuple[Quote, str]
    ]  # (quote, side) at this close: the legs' marks are the quotes' reference prices
    premium_unit: float  # ₹ per unit received (+) or paid (−) at the fills
    loss_unit: float | None  # defined max loss per unit at the fills, before costs
    iv30: float | None
    iv_pct: float | None
    build: Callable[[int], Built]
    one: Built  # one lot
    n_trade: (
        int  # the registered per-trade lots: min(max_lots, floor(budget ÷ (max loss + costs) of one lot))
    )
    scan: (
        dict[str, Any] | None
    )  # the day's NSE SPAN ranges used for the margin (None: margin_approx's assumptions)


def span(h: Any, sym: str, d: date) -> dict[str, Any] | None:
    fn = getattr(h, "span_params", None)  # a history without SPAN ranges: the labelled assumptions
    return fn(sym, d) if fn is not None else None


def candidate(st: engine.SpecState, x: Day, sym: str, h: Any, ivs: list[float], book: Book,
              next_session: date | None = None,
              expiry_day: Callable[[date], date] = lambda e: e) -> Candidate | None:  # fmt: skip
    """try_enter up to its lots: None when no entry is considered or one is refused (counted in st.skips)."""
    spec, d = st.spec, x.day
    if len(st.open) + len(st.pending) >= spec.max_concurrent:
        return None
    if st.last_entry is not None and (d - st.last_entry).days < spec.min_days_between:
        return None
    exps = [e for e in x.expiries() if spec.dte_min <= (e - d).days <= spec.dte_max
            and (spec.expiry_kind == "any" or x.is_monthly(e))]  # fmt: skip
    exps = [e for e in exps if sum(p.expiry == e for p in st.open + st.pending) < spec.max_per_expiry]
    if not exps:
        return None
    st.entries_considered += 1
    expiry = exps[0]
    if not spec.hold_to_expiry and next_session is not None and next_session >= expiry_day(expiry):
        st.skips["no session left before expiry day"] += 1
        return None
    stats = vol.iv_stats(ivs) if x.iv30 is not None else None
    pct = stats.percentile if stats else None
    if spec.iv_pct_min is not None and (pct is None or pct < spec.iv_pct_min):
        st.skips["IV percentile unknown" if pct is None else "IV percentile below the filter"] += 1
        return None
    fwd = px.forward(x, expiry, book.rate)
    if fwd is None:
        st.skips["no forward"] += 1
        return None
    picked = engine.choose_legs(spec, x, expiry, fwd, book)
    if isinstance(picked, str):
        st.skips[picked] += 1
        return None
    lot = h.lot(sym, expiry, d)
    if not lot:
        st.skips["lot size unknown"] += 1
        return None
    if ch.unknown_keys(d):
        st.skips["charges unknown on the trade date"] += 1
        return None
    t = (expiry - d).days / 365
    fills = [(q, side, px.fill(q.ref, side, q.contracts, book)) for q, side in picked]  # type: ignore[arg-type]
    prem_unit = sum((f if side == "sell" else -f) for _, side, f in fills)
    if spec.kind in CREDIT and prem_unit <= 0:
        st.skips["no credit after slippage"] += 1
        return None
    loss_unit = engine.max_loss_per_unit(spec, picked, prem_unit)
    if loss_unit is not None and loss_unit <= 0:
        st.skips["no risk at these prices (data error?)"] += 1
        return None
    scan = span(h, sym, d)

    def build(lots: int) -> Built:
        legs = [engine.Leg(q.right, q.strike, side, lots * lot, f, px.leg_iv(q, fwd, t, book.rate), q.ref)  # type: ignore[arg-type]
                for q, side, f in fills]  # fmt: skip
        costs = ch.order_costs([ch.TradeLeg("option", lg.side, lg.entry, lg.qty) for lg in legs], d,  # type: ignore[arg-type]
                               book.brokerage_per_order).total  # fmt: skip
        return legs, costs, engine.margin(spec, sym, legs, fwd, t, book, scan)

    one = build(1)
    n = 1 if spec.role == "overlay" else min(
        book.max_lots, math.floor(book.capital * book.max_loss_pct / 100 / (loss_unit * lot + one[1]))  # type: ignore[operator]
    )  # fmt: skip
    return Candidate(
        spec, sym, d, expiry, lot, picked, prem_unit, loss_unit, x.iv30, pct, build, one, n, scan
    )


def fit_margin(
    c: Candidate, lots: int, book_open: Sequence[engine.Position], book: Book
) -> tuple[int, Built] | str:
    """The engine's margin rule over `book_open` (every open position of the book, of any spec): lots fall one at a
    time while their entry margins plus this trade's exceed the capital. A refusal reason (the engine's) or (lots,
    the legs, costs and margin at those lots)."""
    needs_margin = c.spec.kind != "covered_call"
    if needs_margin and any(p.margin is None for p in book_open):
        return "margin of an open position unknown"  # unknown is never counted as 0
    used = sum(p.margin or 0.0 for p in book_open)
    built = c.one
    while lots >= 1:
        built = c.build(lots)
        if not needs_margin or (built[2] is not None and used + built[2] <= book.capital):
            break
        if built[2] is None:
            return "margin unknown (a leg's IV cannot be solved)"
        lots -= 1
    if lots < 1:
        return "max loss or margin above the budget for one lot"
    return lots, built


def place(st: engine.SpecState, c: Candidate, lots: int, built: Built) -> engine.Position:
    """The position try_enter appends (st.open, st.last_entry)."""
    legs, costs, mg = built
    units = lots * c.lot
    p = engine.Position(c.spec.key, c.symbol, c.expiry, c.day, (c.expiry - c.day).days, lots, c.lot, legs,
                        c.premium_unit * units, None if c.loss_unit is None else c.loss_unit * units, mg, costs,
                        c.iv30, c.iv_pct, None)  # fmt: skip
    st.open.append(p)
    st.last_entry = c.day
    return p

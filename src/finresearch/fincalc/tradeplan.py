"""Trade-plan arithmetic for F&O strategies (#317): what kind of strategy a set of legs is, strikes by delta on a live
chain, lots from the risk budget, limit-price guidance from bid/ask, stop-loss and target levels in premium and in
underlying terms, and the verdict rule. Pure functions (no I/O); signals.tradeplan assembles the card.

Conventions: a leg's `qty` is signed units (lots × lot size, + bought, − sold). Rupee figures are for the whole
position. "Net premium" per share is Σ(sold prices) − Σ(bought prices) of a one-lot-each combination: positive is a
credit received, negative a debit paid (only defined when every leg has the same number of lots).

Underlying levels are model levels: the position is repriced with Black-Scholes at today's time to expiry and each
leg's current implied volatility, so "the stop is hit if NIFTY trades at X" means "if it got there today, with IVs
unchanged". Real IVs rise in a sell-off (skew), so a put-side stop is usually hit earlier than the level says. The
expiry level (payoff only) is exact. Nothing here is a forecast.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from finresearch.fincalc import options as o

HARD_GATES = frozenset({"Defined risk", "Max loss within your F&O budget", "Not a same-day expiry"})
ENTER, WAIT, AVOID = "ENTER", "WAIT", "AVOID"
# #350: every check passes but no matched spec is a candidate: paper trading only, 0 real-money lots (never ENTER)
PAPER = "PAPER"
PAPER_ONLY = "NO PROVEN EDGE — paper only (0 real lots)"


@dataclass(frozen=True)
class PlanLeg:
    right: str  # call | put | future
    strike: float
    side: str  # buy | sell
    lots: int = 1


# --------------------------------------------------------------------------- what the legs are
def classify(legs: Sequence[PlanLeg]) -> str:
    """The strategy's kind from its legs' shape: put_credit, call_credit, debit_call, debit_put, iron_condor,
    iron_butterfly, long_/short_ call/put/straddle/strangle, long_/short_future, or "custom"."""
    if not legs:
        return "custom"
    if any(x.right == "future" for x in legs):
        return f"{'long' if legs[0].side == 'buy' else 'short'}_future" if len(legs) == 1 else "custom"
    if len({x.lots for x in legs}) != 1:
        return "custom"
    if len(legs) == 1:
        x = legs[0]
        return f"{'long' if x.side == 'buy' else 'short'}_{x.right}"
    if len(legs) == 2:
        a, b = sorted(legs, key=lambda x: (x.right, x.strike))
        if a.right == b.right and a.side != b.side and a.strike != b.strike:
            low_bought = a.side == "buy"
            if a.right == "call":
                return "debit_call" if low_bought else "call_credit"
            return "put_credit" if low_bought else "debit_put"
        if a.right != b.right and a.side == b.side:
            call, put = (a, b) if a.right == "call" else (b, a)
            if call.strike == put.strike:
                return f"{'long' if a.side == 'buy' else 'short'}_straddle"
            if call.strike > put.strike:
                return f"{'long' if a.side == 'buy' else 'short'}_strangle"
        return "custom"
    if len(legs) == 4:
        puts = [x for x in legs if x.right == "put"]
        calls = [x for x in legs if x.right == "call"]
        if (
            len(puts) == 2
            and len(calls) == 2
            and classify(puts) == "put_credit"
            and classify(calls) == "call_credit"
        ):
            short_put = next(x.strike for x in puts if x.side == "sell")
            short_call = next(x.strike for x in calls if x.side == "sell")
            if short_put < short_call:
                return "iron_condor"
            if short_put == short_call:
                return "iron_butterfly"
    return "custom"


CREDIT_KINDS = frozenset({"put_credit", "call_credit", "iron_condor", "iron_butterfly", "short_straddle",
                          "short_strangle", "short_call", "short_put"})  # fmt: skip


def is_credit(kind: str) -> bool:
    return kind in CREDIT_KINDS


# --------------------------------------------------------------------------- strikes by delta on a live chain
@dataclass(frozen=True)
class ChainQuote:
    """One side of one strike in a live chain: NSE's IV (% a year), last price, bid/ask, OI and volume."""

    strike: float
    right: str
    iv: float | None
    last: float | None
    bid: float | None = None
    ask: float | None = None
    oi: float | None = None
    volume: float | None = None

    @property
    def traded(self) -> bool:
        return bool(self.last and self.last > 0 and ((self.oi or 0) > 0 or (self.volume or 0) > 0))


def delta(q: ChainQuote, spot: float, t: float, rate: float) -> float | None:
    """Black-Scholes delta at the chain's IV for the strike (None without an IV)."""
    if not q.iv or q.iv <= 0 or q.right == "future":
        return None
    return o.greeks(q.right, spot, q.strike, t, rate, q.iv / 100).delta  # type: ignore[arg-type]


def pick_by_delta(quotes: Sequence[ChainQuote], right: str, target: float, spot: float, t: float, rate: float,
                  otm_only: bool = True) -> ChainQuote | None:  # fmt: skip
    """The traded strike whose |delta| is nearest `target`; ties go to the strike farther from the money (the same
    rule as the backtester's backtest.pricing.by_delta, here on the live chain's IVs). OTM only unless told."""
    best: tuple[float, float, ChainQuote] | None = None
    for q in quotes:
        if q.right != right or not q.traded:
            continue
        if otm_only and ((right == "put" and q.strike > spot) or (right == "call" and q.strike < spot)):
            continue
        d = delta(q, spot, t, rate)
        if d is None:
            continue
        key = (round(abs(abs(d) - target), 9), -abs(q.strike - spot))
        if best is None or key < best[:2]:
            best = (*key, q)
    return best[2] if best else None


def wing(quotes: Sequence[ChainQuote], right: str, anchor: float, target: float) -> ChainQuote | None:
    """The traded strike nearest `target` beyond `anchor` (below it when target < anchor, else above)."""
    below = target < anchor
    pool = [
        q
        for q in quotes
        if q.right == right and q.traded and (q.strike < anchor if below else q.strike > anchor)
    ]
    return min(pool, key=lambda q: (abs(q.strike - target), abs(q.strike - anchor))) if pool else None


# --------------------------------------------------------------------------- size
@dataclass(frozen=True)
class Size:
    multiple: int  # how many times the base legs fit the budget (0 = not even once)
    suggested: int  # the size the plan uses (paper only without a proven edge): n, one formula (#355)
    budget: float
    risk_per_multiple: float | None  # max loss + entry costs of one multiple; None = unlimited
    reason: str
    real_lots: int = 0  # real-money size (#350): 0 unless the matched spec is a candidate
    # #395: the open-book term that cut `suggested` below `multiple` (book | concurrent | shock), and the margin
    # term's reason on real money (why real_lots is below suggested, or unknown)
    binding: str | None = None
    real_reason: str = ""


# #358 (interim open-book cap; P4.2 adds the common-shock and margin terms). Registered values, stated in the issue
# before any use: the open book may risk at most 8% of F&O capital, a month-to-date F&O loss of 6% of it stops every
# card until the next month, and a naked upside is shocked by +25%. No stress scenario is registered yet (P4.2), so
# the open risk of an undefined-risk position is the larger of the loss at S = 0 and at S × (1 + UP_SHOCK).
BOOK_BUDGET_PCT = 8.0
MONTH_LOSS_STOP_PCT = 6.0
UP_SHOCK = 0.25

# P4.2 (#395): the common-shock and margin terms. Registered values, from the issue's reviewed text (20-Oct-2026):
# the book after a new trade may lose at most SHOCK_BUDGET_PCT of F&O capital at any of NIFTY's one-session moves
# SHOCK_MOVES (other underlyings move β × it, IVs follow the spot-vol response, one session passes: #394's engine), and
# margin may use at most MARGIN_UTIL_MAX of the margin capacity. CASH_SHARE_MIN = c_min, the share of margin that must
# be cash: the exchanges' 50 % cash-equivalent rule for collateral [U: the issue marks it "verify the rule"].
SHOCK_MOVES = (-0.03, -0.05, -0.10, 0.05)
SHOCK_BUDGET_PCT = 6.0
MARGIN_UTIL_MAX = 0.5
CASH_SHARE_MIN = 0.5


def shock_lots(book: Mapping[float, float | None] | None, one: Mapping[float, float | None] | None,
               limit: float, moves: Sequence[float] = SHOCK_MOVES) -> tuple[float | None, str]:  # fmt: skip
    """Lots the common-shock budget allows (#395): the open book's P&L L_book(m) and one lot's L_1(m) (₹, signed) at
    each registered move; P&L is linear in lots, so n lots add n × L_1(m). Accept n only if L_book(m) + n L_1(m) ≥
    −limit at every m: n = min over the moves where one lot loses of floor((limit + L_book(m)) ÷ −L_1(m)). A book
    already beyond the limit at any move takes nothing new (not even a hedge). None when any value is unknown; inf
    when one lot loses at no move."""
    if book is None or one is None or any(book.get(m) is None or one.get(m) is None for m in moves):
        return (
            None,
            "the book's or this trade's loss under the common shock is unknown, so it has no known room",
        )
    worst = min(moves, key=lambda m: book[m])  # type: ignore[arg-type,return-value]
    if book[worst] <= -limit:  # type: ignore[operator]
        return 0, (f"the open book already loses ₹{-book[worst]:,.0f} at a {worst:+.0%} NIFTY move, at or beyond the "  # type: ignore[operator]
                   f"₹{limit:,.0f} common-shock budget")  # fmt: skip
    bound: tuple[float, float] | None = None
    for m in moves:
        if one[m] < 0:  # type: ignore[operator]
            n = max(0, math.floor((limit + book[m]) / -one[m]))  # type: ignore[operator]
            bound = (n, m) if bound is None or n < bound[0] else bound
    if bound is None:
        return math.inf, "this trade loses at none of the shock moves"
    n, m = bound
    return n, (f"common shock at {m:+.0%}: ₹{limit:,.0f} budget, the open book's P&L ₹{book[m]:,.0f}, "
               f"₹{-one[m]:,.0f} loss per set = room for {n:g}")  # type: ignore[operator]  # fmt: skip


def margin_utilisation(margin_used: float, im_new: float, cash: float, collateral: float,
                       c_min: float = CASH_SHARE_MIN) -> float:  # fmt: skip
    """u = max((used + IM_new) ÷ (cash + collateral after haircut), (used + IM_new) × c_min ÷ cash) (#395)."""
    x = margin_used + im_new
    return max(x / (cash + collateral), x * c_min / cash if cash > 0 else math.inf)


def margin_lots(margin_used: float | None, im_one: float | None, cash: float | None, collateral: float | None,
                u_max: float = MARGIN_UTIL_MAX, c_min: float = CASH_SHARE_MIN) -> tuple[float | None, str]:  # fmt: skip
    """Lots the margin cap allows (#395): u ≤ u_max ⇔ used + n IM_1 ≤ u_max × min(cash + collateral, cash ÷ c_min),
    so n = floor((u_max × min(cash + collateral, cash ÷ c_min) − used) ÷ IM_1), floored at 0. `collateral` is after
    the broker's haircut. None when any input is unknown (never counted as 0)."""
    if margin_used is None or im_one is None or cash is None or collateral is None:
        return (
            None,
            "the margin in use, this trade's margin or the funds are unknown, so margin has no known room",
        )
    if cash <= 0:
        return 0, f"no cash: margin needs a cash share of at least {c_min:.0%}"
    cap = u_max * min(cash + collateral, cash / c_min)
    if im_one <= 0:
        return math.inf, "this trade needs no margin"
    n = max(0, math.floor((cap - margin_used) / im_one))
    return n, (f"margin: {u_max:.0%} of ₹{min(cash + collateral, cash / c_min):,.0f} capacity − ₹{margin_used:,.0f} in "
               f"use, ₹{im_one:,.0f} per set = room for {n}")  # fmt: skip


@dataclass(frozen=True)
class Shock:
    """The common-shock inputs (#395): the open book's P&L and one set of the new legs' P&L (₹, signed) at each of
    SHOCK_MOVES; a map (or a value in it) None = unknown, never 0."""

    book: Mapping[float, float | None] | None
    one: Mapping[float, float | None] | None
    notes: tuple[str, ...] = ()
    betas: tuple[
        Any, ...
    ] = ()  # the fincalc.beta estimates the shock used (shown on the card; NIFTY's 1 is not)


@dataclass(frozen=True)
class Funds:
    """The real book's funds for the margin term (#395): Groww's cash, margin in use and collateral after haircut, and
    one set of the new legs' initial margin (₹; None = unknown, never 0)."""

    cash: float | None
    margin_used: float | None
    collateral: float | None
    im_one: float | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenBook:
    """What is already open (#358), for one card. `open_risk` = OR, the Σ of position_risk over open paper and Groww
    positions (None = unknown: never counted as room); `open_same` = open positions of this recipe / shape, against the
    backtester's `max_concurrent`; month-to-date F&O P&L after costs (realised + marked), the paper book's for paper
    cards and the real book's for real-money permission (None = unknown)."""

    capital: float
    open_risk: float | None
    open_same: int = 0
    max_concurrent: int | None = None
    mtd_paper: float | None = None
    mtd_real: float | None = None
    notes: tuple[str, ...] = ()
    # #395: None = the term is not evaluated (pure callers only; signals.tradeplan.book_for always sets both)
    shock: Shock | None = None
    funds: Funds | None = None

    @property
    def budget(self) -> float:
        return self.capital * BOOK_BUDGET_PCT / 100

    @property
    def shock_budget(self) -> float:
        return self.capital * SHOCK_BUDGET_PCT / 100

    def month_stopped(self, real: bool = False) -> bool:
        """True when that book's month-to-date P&L is at or below −MONTH_LOSS_STOP_PCT of capital, or unknown."""
        mtd = self.mtd_real if real else self.mtd_paper
        return mtd is None or mtd <= -self.capital * MONTH_LOSS_STOP_PCT / 100


def position_risk(legs: Sequence[o.Leg], spot: float | None) -> float | None:
    """One open position's contribution to OR (#358), ₹ (≥ 0), from its legs at their entry prices (premiums; signed
    qty): |max loss at expiry| when bounded above (the payoff is piecewise linear, so its minimum on [0, ∞) is at 0 or
    a strike); otherwise the larger of that and the loss at S × (1 + UP_SHOCK). None when the shock needs a spot that
    is unknown."""
    if not legs:
        return 0.0
    kinks = [0.0, *(x.strike for x in legs if x.right != "future")]
    up_slope = sum(x.qty for x in legs if x.right in ("call", "future"))
    if up_slope < 0:
        if not spot:
            return None
        kinks.append(spot * (1 + UP_SHOCK))
    return max(0.0, -min(o.payoff(legs, k) for k in kinks))


def size(max_loss_one: float | None, costs_one: float, budget: float, proven: bool, cap: int = 100,
         book: OpenBook | None = None) -> Size:  # fmt: skip
    """Lots from the risk budget: floor(budget ÷ (max loss + entry costs of the base legs)), as the backtester sizes
    (backtest.engine.try_enter): one formula n = floor(B / (|ML| + c)) (#355). With the open book (#358):
    n = min(that, floor((B_book − OR) ÷ (|ML| + c))), floored at 0, and 0 once `max_concurrent` of this recipe are
    open. #395 adds the common-shock term, floor((B_shock − |L_book|) ÷ |dL/dn|) taken per move (`shock_lots`), to n,
    and the margin term (`margin_lots`, the real book's funds) to real lots only; an unknown term sizes 0.
    Real money (#350) only when the matched spec is a candidate (`proven`): otherwise n is the paper size and
    real lots are 0. `multiple` stays the trade budget's alone (a full book is not an over-budget trade)."""
    if max_loss_one is None:
        return Size(0, 0, budget, None, "the loss is unlimited, so no size fits a budget")
    risk = abs(max_loss_one) + max(costs_one, 0.0)
    if budget <= 0:
        return Size(0, 0, budget, risk, "no F&O budget: set F&O capital and the max-loss % on the profile")
    m = min(cap, math.floor(budget / risk)) if risk > 0 else 0
    if m < 1:
        return Size(
            0, 0, budget, risk, f"one set of these legs risks ₹{risk:,.0f}, above your ₹{budget:,.0f} budget"
        )
    why = f"₹{budget:,.0f} budget ÷ ₹{risk:,.0f} risk per set (max loss + entry costs) = {m}"
    n, binding = m, None
    if book is not None:
        if book.open_risk is None:
            n, binding, more = (
                0,
                "book",
                "the risk of your open positions is unknown, so the open book has no known room: 0",
            )
        elif book.max_concurrent is not None and book.open_same >= book.max_concurrent:
            n, binding, more = 0, "concurrent", (f"{book.open_same} of this recipe already open, the backtest's limit "
                                                 f"of {book.max_concurrent} at a time: 0")  # fmt: skip
        else:
            room = book.budget - book.open_risk
            n = max(0, min(m, math.floor(room / risk)))
            binding = "book" if n < m else None
            more = (f"open book: ₹{book.budget:,.0f} ({BOOK_BUDGET_PCT:g}% of F&O capital) − ₹{book.open_risk:,.0f} "
                    f"already at risk in open positions = room for {max(0, math.floor(room / risk))}")  # fmt: skip
        why += f"; {more}"
        if book.shock is not None:  # #395: the common-shock term (B_shock = SHOCK_BUDGET_PCT of F&O capital)
            sn, swhy = shock_lots(book.shock.book, book.shock.one, book.shock_budget)
            if sn is None and book.shock.notes:
                swhy += " (" + "; ".join(book.shock.notes) + ")"
            if binding is None and (sn is None or sn < n):
                binding = "shock"
            n = 0 if sn is None else min(n, int(min(sn, n)))
            why += f"; {swhy}"
    real, real_why = (n if proven else 0), ""
    if not proven:
        why += "; no strategy that passed out of sample matches, so paper only: 0 real-money lots"
    elif book is not None and book.funds is not None:  # #395: the margin term, real money only
        f = book.funds
        mn, real_why = margin_lots(f.margin_used, f.im_one, f.cash, f.collateral)
        if mn is None and f.notes:
            real_why += " (" + "; ".join(f.notes) + ")"
        real = 0 if mn is None else min(real, int(min(mn, real)))
    return Size(m, n, budget, risk, why, real_lots=real, binding=binding, real_reason=real_why)


# --------------------------------------------------------------------------- limit prices from bid/ask
def limit_guidance(legs: Sequence[PlanLeg], quotes: Sequence[ChainQuote | None], lot: int) -> dict[str, Any]:
    """Per leg: bid, ask, mid and the limit range (mid = patient, the far side = fill now). The net per share only
    when every leg has a bid and an ask and the same lots; otherwise "unknown" (a last trade is shown as a reference,
    never as a quote). Slippage (#324) = what filling now gives up against the mids: |far side − mid| per share a leg,
    and in rupees Σ |far − mid| × lots × lot size; None unless every leg has a bid and an ask (unknown, never 0)."""
    rows, known = [], True
    net_mid = net_now = slip_inr = 0.0
    for x, q in zip(legs, quotes, strict=True):
        bid = q.bid if q and q.bid and q.bid > 0 else None
        ask = q.ask if q and q.ask and q.ask > 0 else None
        r: dict[str, Any] = {"right": x.right, "strike": x.strike, "side": x.side, "lots": x.lots, "bid": bid,
                             "ask": ask, "last": q.last if q else None, "mid": None, "limit_from": None,
                             "limit_to": None, "spread_pct": None, "slippage": None, "status": "unknown"}  # fmt: skip
        if bid is not None and ask is not None and ask >= bid:
            mid = round((bid + ask) / 2, 2)
            far = ask if x.side == "buy" else bid
            r.update(
                mid=mid,
                limit_from=mid,
                limit_to=far,
                spread_pct=round((ask - bid) / mid * 100, 2),
                slippage=round(abs(far - mid), 2),
                status="ok",
            )
            slip_inr += abs(far - mid) * x.lots * lot
            sign = 1 if x.side == "sell" else -1
            net_mid += sign * mid
            net_now += sign * far
        else:
            known = False
        rows.append(r)
    same_lots = len({x.lots for x in legs}) == 1
    out: dict[str, Any] = {"legs": rows, "status": "ok" if known and same_lots else "unknown",
                           "net_mid": None, "net_fill_now": None, "notes": [],
                           "slippage_inr": round(slip_inr, 2) if known else None}  # fmt: skip
    if known and same_lots:
        out["net_mid"], out["net_fill_now"] = round(net_mid, 2), round(net_now, 2)
    elif not known:
        out["notes"].append("a leg has no live bid/ask (no chain with bid/ask could be read, or the strike is "
                            "untraded): the entry range is unknown; last trades are shown only as a reference")  # fmt: skip
    wide = [r for r in rows if r["spread_pct"] is not None and r["spread_pct"] > 10]
    if wide:
        out["notes"].append("wide bid/ask on " + ", ".join(f"{r['strike']:g} {r['right']}" for r in wide)
                            + " (over 10% of the mid): a thin strike, expect to give up part of the spread")  # fmt: skip
    return out


def needs_bidask(quotes: Sequence[ChainQuote | None], rights: Sequence[str]) -> bool:
    """True when an option leg (not a future) lacks a positive bid or ask: limit guidance needs another chain."""
    return any(r != "future" and not (q and q.bid and q.bid > 0 and q.ask and q.ask > 0)
               for q, r in zip(quotes, rights, strict=True))  # fmt: skip


# --------------------------------------------------------------------------- exits: stop, target, time
@dataclass(frozen=True)
class ExitPlan:
    profit_pct: float | None  # take profit at this % of the entry net premium (credit received or debit paid)
    stop_pct: float | None  # stop at a loss of this % of the entry net premium
    exit_dte: int | None  # close at the first close with days to expiry <= this
    before_expiry_day: bool  # close on the last session before expiry day at the latest
    hold_to_expiry: bool
    source: str  # "spec:<key>" (the pre-registered recipe) or "rule of thumb"


RULE_OF_THUMB = ("Rule of thumb, not backtested: credit trades take profit at 50% of the credit and stop at a loss of "
                 "2× the credit (the common retail recipe the backtest's sellers use); debit trades take profit at "
                 "+50% and stop at −50% of the debit; close 7 days before a monthly expiry, or on the last session "
                 "before expiry day for a weekly.")  # fmt: skip


def default_exits(kind: str, dte: int) -> ExitPlan:
    weekly = dte <= 9
    if kind.endswith("_future"):
        return ExitPlan(None, None, None if weekly else 7, True, False, "rule of thumb")
    if is_credit(kind):
        return ExitPlan(50.0, 200.0, None if weekly else 7, True, False, "rule of thumb")
    return ExitPlan(50.0, 50.0, None if weekly else 7, True, False, "rule of thumb")


def spec_exits(spec: Any) -> ExitPlan:
    """The recipe's own exits (backtest.specs.Spec): stop_mult is a multiple of the credit (sellers)."""
    stop = None if spec.stop_mult is None else spec.stop_mult * 100
    return ExitPlan(spec.profit_take_pct, stop, spec.exit_dte, not spec.hold_to_expiry, spec.hold_to_expiry,
                    f"spec:{spec.key}")  # fmt: skip


def model_pnl(
    legs: Sequence[o.Leg], ivs: Sequence[float | None], s: float, t: float, rate: float
) -> float | None:
    """The position's P&L (₹, before costs) if the underlying were at `s` now with each leg's IV unchanged."""
    total = 0.0
    for x, iv in zip(legs, ivs, strict=True):
        if x.right == "future":
            total += (s - x.strike) * x.qty
            continue
        if not iv:
            return None
        total += (o.bs_price(x.right, s, x.strike, t, rate, iv) - x.premium) * x.qty
    return total


def _solve(fn, spot: float, level: float, side: int, reach: float = 0.4, steps: int = 320) -> float | None:
    """The nearest price on one side of spot (side −1 below, +1 above) where fn crosses `level`, by a grid scan then
    bisection; None when it does not within ±reach."""
    f0 = fn(spot)
    if f0 is None:
        return None
    above = f0 >= level
    prev_s = spot
    for i in range(1, steps + 1):
        s = spot * (1 + side * reach * i / steps)
        v = fn(s)
        if v is None:
            return None
        if (v >= level) != above:
            lo, hi = prev_s, s
            for _ in range(60):
                mid = (lo + hi) / 2
                vm = fn(mid)
                if vm is None:
                    return None
                if (vm >= level) == above:
                    lo = mid
                else:
                    hi = mid
            return round((lo + hi) / 2, 2)
        prev_s = s
    return None


def levels(legs: Sequence[o.Leg], ivs: Sequence[float | None], spot: float, t: float, rate: float,
           plan: ExitPlan, max_loss: float | None) -> dict[str, Any]:  # fmt: skip
    """Stop and target in rupees, in premium per share and as underlying levels (now, at today's IVs, and at
    expiry). `legs` carry the planned entry prices as premiums."""
    net = sum(x.premium * x.qty for x in legs if x.right != "future")  # + paid, − received
    base = abs(net)
    units = {abs(x.qty) for x in legs if x.right != "future"}
    unit = units.pop() if len(units) == 1 else None
    credit = net < 0
    out: dict[str, Any] = {"entry_net": round(-net, 2), "credit": credit, "per_share_unit": unit, "stop": None,
                           "target": None, "notes": []}  # fmt: skip
    payoff = lambda s: o.payoff(legs, s)  # noqa: E731
    now = lambda s: model_pnl(legs, ivs, s, t, rate)  # noqa: E731

    def block(rupees: float, sign: int) -> dict[str, Any]:
        level = sign * rupees
        # credit: the cost to buy the combination back = credit − P&L per share; debit: its value = debit + P&L
        pnl = sign * rupees
        per = None if unit is None or not base else round(base / unit + (-pnl if credit else pnl) / unit, 2)
        return {"rupees": round(rupees, 2), "premium_per_share": per,
                "underlying_now": {"below": _solve(now, spot, level, -1), "above": _solve(now, spot, level, 1)},
                "underlying_at_expiry": {"below": _solve(payoff, spot, level, -1),
                                         "above": _solve(payoff, spot, level, 1)}}  # fmt: skip

    if plan.stop_pct is not None and base:
        loss = plan.stop_pct / 100 * base
        if max_loss is not None and loss >= abs(max_loss):
            out["notes"].append(f"a {plan.stop_pct:g}% stop (₹{loss:,.0f}) is at or beyond the maximum loss "
                                f"(₹{abs(max_loss):,.0f}): the defined maximum loss is the stop")  # fmt: skip
            loss = abs(max_loss)
        out["stop"] = block(loss, -1)
    elif max_loss is not None:
        out["notes"].append("no premium-based stop for this position: the maximum loss is the stop")
    if plan.profit_pct is not None and base:
        out["target"] = block(plan.profit_pct / 100 * base, 1)
    return out


# --------------------------------------------------------------------------- evidence and the verdict
# #355: roughly delta-neutral structures, whose backtest sign is about the premium, not the index path; only these can
# be judged harmful. Everything else (one-sided credit spreads, debit spreads, single options, futures) is directional.
DELTA_NEUTRAL_KINDS = frozenset({"iron_condor", "iron_butterfly", "short_straddle", "short_strangle"})
HARMFUL_T = -2.0  # expiry-clustered t at or below this AND the bootstrap CI's upper bound below 0 (#355)
DIRECTIONAL_NOTE = "directional — reflects the index path, not the structure"


def evidence_level(row: dict[str, Any] | None, kind: str) -> str:
    """A saved backtest result row (backtest.store.run_json strategies[]) for legs of `kind` → proven | harmful |
    inconclusive | directional | none (#355).
    proven = a candidate (passed every pre-registered out-of-sample criterion) of a delta-neutral kind;
    directional = any judged row of a directional kind: its sign mixes the premium with the index path, so it gives
    neither AVOID nor ENTER; harmful = failed with clustered t ≤ −2 AND the 95% bootstrap CI's upper bound < 0;
    inconclusive = failed otherwise (an unknown t or CI is inconclusive, never harmful or safe);
    none = not judged (no data, an overlay, in-sample only) or no matching strategy."""
    if not row or row.get("status") not in ("candidate", "fails"):
        return "none"
    if kind not in DELTA_NEUTRAL_KINDS:
        return "directional"
    if row["status"] == "candidate":
        return "proven"
    w = (row.get("windows") or {}).get("oos") or {}
    t, hi = w.get("t_stat"), w.get("ci_high")
    if t is not None and hi is not None and t <= HARMFUL_T and hi < 0:
        return "harmful"
    return "inconclusive"


def evidence_text(row: dict[str, Any] | None, level: str) -> str:
    if not row:
        return "No pre-registered backtest covers this strategy: no evidence of an edge either way."
    w = (row.get("windows") or {}).get("oos") or {}
    e, t, n = w.get("expectancy"), w.get("t_stat"), w.get("trades")
    lo, hi = w.get("ci_low"), w.get("ci_high")
    stats = ", ".join(x for x in (None if e is None else f"expectancy ₹{e:,.0f}/trade",
                                  None if t is None else f"t={t:.1f}",
                                  None if lo is None or hi is None else f"95% CI ₹{lo:,.0f} to ₹{hi:,.0f}",
                                  None if n is None else f"{n} trades") if x)  # fmt: skip
    span = f" ({w['from']} → {w['to']})" if w.get("from") and w.get("to") else ""
    status = "PASSED" if row.get("status") == "candidate" else "FAILED"
    head = {"proven": "PASSED every pre-registered out-of-sample criterion",
            "harmful": "FAILED out-of-sample and HARMFUL (t ≤ −2 and the 95% CI below zero)",
            # #357: what the reader needs first is that the interval contains zero, not the word FAILED
            "inconclusive": ("Inconclusive (95% CI spans 0): FAILED out-of-sample, no edge distinguishable from zero "
                             "either way" if lo is not None and hi is not None and lo < 0 < hi else
                             "Inconclusive (CI unknown): FAILED out-of-sample, no edge distinguishable from zero"
                             if lo is None or hi is None else
                             "Inconclusive (the t-statistic and the 95% CI disagree): FAILED out-of-sample"),
            "directional": f"{status} out-of-sample, {DIRECTIONAL_NOTE}",
            "none": f"Not judged out of sample ({row.get('status', 'unknown')})"}[level]  # fmt: skip
    return f"{head}: {stats}{span}." if stats else f"{head}."


def verdict(failed: Sequence[str], evidence: str) -> tuple[str, str | None]:
    """(action, qualifier), #350/#355. AVOID when a hard gate fails or the matching backtest is harmful; WAIT when any
    other check fails or is unknown; ENTER only when everything passes AND the evidence is proven (the matched spec is
    a candidate). Everything passing on inconclusive, directional or no evidence gives PAPER, "NO PROVEN EDGE — paper
    only (0 real lots)": flipping the sign of an insignificant expectancy never changes it."""
    if HARD_GATES & set(failed) or evidence == "harmful":
        return AVOID, None
    if failed:
        return WAIT, None
    return (ENTER, None) if evidence == "proven" else (PAPER, PAPER_ONLY)

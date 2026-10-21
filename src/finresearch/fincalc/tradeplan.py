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
from collections.abc import Sequence
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
    suggested: (
        int  # the paper size the plan uses: the multiple, or half of it (at least 1) without a proven edge
    )
    budget: float
    risk_per_multiple: float | None  # max loss + entry costs of one multiple; None = unlimited
    reason: str
    real_lots: int = 0  # real-money size (#350): 0 unless the matched spec is a candidate


def size(max_loss_one: float | None, costs_one: float, budget: float, proven: bool, cap: int = 100) -> Size:
    """Lots from the risk budget: floor(budget ÷ (max loss + entry costs of the base legs)), as the backtester sizes
    (backtest.engine.try_enter). Real money (#350) only when the matched spec is a candidate (`proven`): otherwise 0
    real lots, and the paper size is half of it, at least one when one fits."""
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
    sug = m if proven else max(1, m // 2)
    why = f"₹{budget:,.0f} budget ÷ ₹{risk:,.0f} risk per set (max loss + entry costs) = {m}"
    if not proven:
        why += (f"; no strategy that passed out of sample matches, so 0 real-money lots and a paper size of half of "
                f"it: {sug}")  # fmt: skip
    return Size(m, sug, budget, risk, why, real_lots=m if proven else 0)


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
def evidence_level(row: dict[str, Any] | None) -> str:
    """A saved backtest result row (backtest.store.run_json strategies[]) → proven | negative | unproven | none.
    negative = failed out of sample with an expectancy after costs at or below zero; unproven = failed, but the
    expectancy was positive (not significant, or another criterion failed); none = not judged (no data, an overlay,
    in-sample only) or no matching strategy."""
    if not row:
        return "none"
    if row.get("status") == "candidate":
        return "proven"
    if row.get("status") != "fails":
        return "none"
    e = ((row.get("windows") or {}).get("oos") or {}).get("expectancy")
    return "unproven" if e is None or e > 0 else "negative"


def evidence_text(row: dict[str, Any] | None, level: str) -> str:
    if not row:
        return "No pre-registered backtest covers this strategy: no evidence of an edge either way."
    w = (row.get("windows") or {}).get("oos") or {}
    e, t, n = w.get("expectancy"), w.get("t_stat"), w.get("trades")
    stats = ", ".join(x for x in (None if e is None else f"expectancy ₹{e:,.0f}/trade",
                                  None if t is None else f"t={t:.1f}", None if n is None else f"{n} trades") if x)  # fmt: skip
    span = f" ({w['from']} → {w['to']})" if w.get("from") and w.get("to") else ""
    head = {"proven": "PASSED every pre-registered out-of-sample criterion",
            "negative": "FAILED out-of-sample", "unproven": "FAILED out-of-sample (positive but not proven)",
            "none": f"Not judged out of sample ({row.get('status', 'unknown')})"}[level]  # fmt: skip
    return f"{head}: {stats}{span}." if stats else f"{head}."


def verdict(failed: Sequence[str], evidence: str) -> tuple[str, str | None]:
    """(action, qualifier). AVOID when a hard gate fails or the matching backtest is negative; WAIT when any other
    check fails or is unknown; ENTER only when everything passes AND the matched spec is a candidate (evidence
    "proven"). Everything passing without that gives PAPER, "NO PROVEN EDGE — paper only (0 real lots)" (#350)."""
    if HARD_GATES & set(failed) or evidence == "negative":
        return AVOID, None
    if failed:
        return WAIT, None
    return (ENTER, None) if evidence == "proven" else (PAPER, PAPER_ONLY)

"""The VRP in the form this trader could collect (P3.6, #389; vault vrp.md section (2)). Pure.

A short ATM straddle (or a short 25-delta put), 1 lot, sold at the settle of one monthly expiry and held to the next
monthly expiry, where it is cash-settled at the final settlement price:

    un-hedged                Pi_u    = lot x [sum (fill_i) - sum intrinsic_i(S_T)] - costs
    integer-lot band hedge   Pi_band = Pi_u + lot x sum_n H_n (F_n+1 - F_n) - hedge slippage - hedge costs,
                             H_n whole futures lots, re-set to round(Delta_n) at a settle only when |H - Delta_n| >= 0.5
    fractional (benchmark)   H_n = Delta_n every session: "not collectable at 1-2 lots"

Delta_n = the straddle's Black-76 forward delta per unit (call + put) at the session's settles, with ONE volatility
implied from the out-of-the-money leg's settle (an in-the-money leg's settle is often NSE's theoretical price), the
same-expiry future as the forward and fincalc.fno_history.RATE; T = calendar days to expiry / 365. The hedge is the
same-expiry future, so it converges to S_T with the options.

Costs: fincalc.charges' dated rates on each trade date (an unknown rate makes the window unknown, never 0), with the
book's flat brokerage per order; re-run at COLLECTABLE_TODAY's rates as the collectable-today case (the 2026 STT rows
are "unconfirmed"). Option slippage = backtest.costs.slippage (the registered tiers by contracts traded, for premia);
futures slippage = FUT_SLIPPAGE_BPS of the price per unit [U: assumption]. Cash settlement at expiry carries no
charge for the short options or the expiring future here [U: futures final settlement assumed free of STT].
Margin: fincalc.margin_approx's SPAN-style estimate of the option legs at entry (approximate; unknown -> None), and
the carry its cash part forgoes, as backtest.criteria_v2 (cash share x collateral rate x days / 365).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finresearch.backtest import costs as bcosts
from finresearch.backtest import inference
from finresearch.backtest.criteria_v2 import CRITERIA_V2
from finresearch.backtest.specs import BOOK, Book
from finresearch.fincalc import black76 as b76
from finresearch.fincalc import charges, margin_approx
from finresearch.fincalc import fno_history as fh

BAND = 0.5  # lots of net delta that trigger a re-hedge
FUT_SLIPPAGE_BPS = (
    1.0  # per unit, in basis points of the futures price [U: assumption; no futures spreads stored]
)
PUT_DELTA = -0.25
COLLECTABLE_TODAY = date(
    2026, 4, 1
)  # the Finance Bill 2026 rates (STT on options 0.15 %, futures 0.05 %; [U])
BOOTSTRAP_DRAWS, BOOTSTRAP_SEED = 5000, 389
CRASHES = {"2008-09..2009-03": (date(2008, 9, 1), date(2009, 3, 31)),
           "2020-02..2020-06": (date(2020, 2, 1), date(2020, 6, 30))}  # fmt: skip
HEDGES = ("none", "band", "fractional")


@dataclass(frozen=True)
class Mark:
    """One session's settles: the same-expiry future and the trade's call and put at its strike."""

    day: date
    fut: float | None
    call: float | None
    put: float | None
    call_contracts: int | None
    put_contracts: int | None


@dataclass(frozen=True)
class Trade:
    entry: date
    expiry: date
    strike: float
    lot: int | None
    marks: tuple[Mark, ...]  # every stored session from the entry to the last session before the expiry
    final: float | None  # the final settlement price
    rights: tuple[str, ...]  # ("call", "put") for the straddle, ("put",) for the 25-delta put


@dataclass(frozen=True)
class Result:
    entry: date
    net: float | None
    gross: float | None
    costs: float | None
    slippage: float
    hedge_pnl: float
    hedges: tuple
    margin: float | None
    carry: float | None
    net_after_carry: float | None
    reason: str | None = None


def collectable_today_overrides() -> dict[str, Decimal]:
    return {k: v.rate for k, v in charges.rates_as_of(COLLECTABLE_TODAY).items() if v.rate is not None}


def _unknown(t: Trade, reason: str) -> Result:
    return Result(t.entry, None, None, None, 0.0, 0.0, (), None, None, None, reason)


def _iv(t: Trade, m: Mark, rate: float) -> float | None:
    """One volatility from the out-of-the-money leg's settle (put below the forward, call at or above)."""
    if m.fut is None or m.fut <= 0:
        return None
    tt = (t.expiry - m.day).days / 365
    right = "call" if t.strike >= m.fut else "put"
    px = m.call if right == "call" else m.put
    if px is None or tt <= 0:
        return None
    return b76.implied_vol(right, px, m.fut, t.strike, tt, math.exp(-rate * tt)).sigma  # type: ignore[arg-type]


def _delta(t: Trade, m: Mark, sig: float, rate: float) -> float:
    tt = (t.expiry - m.day).days / 365
    df = math.exp(-rate * tt)
    return sum(b76.forward_delta(r, m.fut, t.strike, tt, df, sig) for r in t.rights)  # type: ignore[arg-type, misc]


def _intrinsic(right: str, s: float, k: float) -> float:
    return max(s - k, 0.0) if right == "call" else max(k - s, 0.0)


def pnl(t: Trade, hedge: str, *, rate: float = fh.RATE, overrides: Mapping[str, Decimal | float] | None = None,
        brokerage: float = BOOK.brokerage_per_order, book: Book = BOOK,
        fut_slippage: float | None = None) -> Result:  # fmt: skip
    """The trade's P&L in rupees after costs under `hedge` ("none", "band", "fractional"); unknown with the reason
    when the lot, an entry price, the final settlement, a dated charge or (hedged) a session's mark or IV is missing.
    `fut_slippage` (rupees per unit) replaces FUT_SLIPPAGE_BPS (tests)."""
    if hedge not in HEDGES:
        raise ValueError(f"hedge must be one of {HEDGES}")
    if t.lot is None:
        return _unknown(t, "no dated lot size")
    if not t.marks or t.marks[0].day != t.entry:
        return _unknown(t, "no settles on the entry day")
    m0 = t.marks[0]
    px = {"call": (m0.call, m0.call_contracts), "put": (m0.put, m0.put_contracts)}
    if any(px[r][0] is None or px[r][0] <= 0 for r in t.rights):  # type: ignore[operator]
        return _unknown(t, "no entry settle for a leg")
    if t.final is None:
        return _unknown(t, "no final settlement price")
    lot = t.lot
    slips = {r: bcosts.slippage(px[r][0], px[r][1], book) for r in t.rights}  # type: ignore[arg-type]
    fills = {r: px[r][0] - slips[r] for r in t.rights}  # type: ignore[operator]
    if any(f <= 0 for f in fills.values()):
        return _unknown(t, "slippage exceeds the premium")
    legs = [charges.TradeLeg("option", "sell", fills[r], lot) for r in t.rights]
    try:
        cost = charges.order_costs(legs, t.entry, brokerage, overrides).total
    except charges.ChargesUnknown as e:
        return _unknown(t, f"charges unknown: {e}")
    gross = lot * (sum(fills.values()) - sum(_intrinsic(r, t.final, t.strike) for r in t.rights))
    slippage = lot * sum(slips.values())
    hedge_pnl, hs = 0.0, []
    if hedge != "none":
        h, prev = 0.0, None
        for m in t.marks:
            sig = _iv(t, m, rate)
            if sig is None:
                return _unknown(t, f"no IV for the hedge delta on {m.day}")
            if prev is not None:
                hedge_pnl += lot * h * (m.fut - prev)  # type: ignore[operator]
            target = _delta(t, m, sig, rate)
            new = (
                target
                if hedge == "fractional"
                else (math.floor(target + 0.5) if abs(h - target) >= BAND else h)
            )
            trade = new - h
            if trade:
                slip = fut_slippage if fut_slippage is not None else FUT_SLIPPAGE_BPS / 1e4 * m.fut  # type: ignore[operator]
                side = "buy" if trade > 0 else "sell"
                fill = m.fut + (slip if side == "buy" else -slip)  # type: ignore[operator]
                hedge_pnl -= abs(trade) * lot * slip
                slippage += abs(trade) * lot * slip
                try:
                    cost += charges.order_costs([charges.TradeLeg("future", side, fill, abs(trade) * lot)],  # type: ignore[arg-type]
                                                m.day, brokerage, overrides).total  # fmt: skip
                except charges.ChargesUnknown as e:
                    return _unknown(t, f"charges unknown: {e}")
            h, prev = new, m.fut
            hs.append(int(h) if hedge == "band" else h)
        hedge_pnl += lot * h * (t.final - prev)  # type: ignore[operator]
    net = gross + hedge_pnl - cost
    margin = _margin(t, rate)
    carry = None if margin is None else margin * CRITERIA_V2.cash_share * CRITERIA_V2.collateral_rate * max(
        (t.expiry - t.entry).days, 1) / 365  # fmt: skip
    return Result(t.entry, net, gross + hedge_pnl, cost, slippage, hedge_pnl, tuple(hs), margin, carry,
                  None if carry is None else net - carry)  # fmt: skip


def _margin(t: Trade, rate: float) -> float | None:
    m0 = t.marks[0]
    sig = _iv(t, m0, rate)
    tt = (t.expiry - t.entry).days / 365
    if sig is None or m0.fut is None or tt <= 0:
        return None
    px = {"call": m0.call, "put": m0.put}
    legs = [margin_approx.MarginLeg(r, t.strike, -t.lot, px[r], sig) for r in t.rights]  # type: ignore[arg-type, operator]
    try:
        return margin_approx.estimate(legs, m0.fut * math.exp(-rate * tt), tt, rate, "index").total
    except ValueError:
        return None


def delta_put_strike(puts: Sequence[tuple[float, float, int | None]], fwd: float, t: float, rate: float,
                     target: float = PUT_DELTA, min_contracts: int = fh.MIN_CONTRACTS) -> float | None:  # fmt: skip
    """The strike at or below the forward whose put (its own Black-76 IV) has the forward delta nearest `target`,
    among puts with at least `min_contracts` traded; None when there is none."""
    df = math.exp(-rate * t)
    best = None
    for k, p, n in puts:
        if k > fwd or (n or 0) < min_contracts:
            continue
        sig = b76.implied_vol("put", p, fwd, k, t, df).sigma
        if sig is None:
            continue
        gap = abs(b76.forward_delta("put", fwd, k, t, df, sig) - target)
        if best is None or gap < best[0]:
            best = (gap, k)
    return None if best is None else best[1]


def summarise(rs: Sequence[Result], draws: int = BOOTSTRAP_DRAWS, seed: int = BOOTSTRAP_SEED) -> dict:
    """Mean net, its expiry-clustered BCa interval (one trade per expiry), share negative, crash-period losses and
    the margin view, with the unknown windows counted by reason."""
    k = [r for r in rs if r.net is not None]
    nets = [r.net for r in k]
    crash = {}
    for name, (a, b) in CRASHES.items():
        xs = [r.net for r in k if a <= r.entry <= b]
        crash[name] = {"windows": len(xs), "net": sum(xs) if xs else None, "worst": min(xs) if xs else None}  # type: ignore[type-var, arg-type]
    mk = [r for r in k if r.margin is not None]
    return {"n": len(k), "unknown": len(rs) - len(k),
            "unknown_reasons": dict(Counter(r.reason for r in rs if r.net is None)),
            "mean_net": sum(nets) / len(nets) if nets else None,  # type: ignore[arg-type]
            "ci": inference.cluster_bca(nets, [r.entry.isoformat() for r in k], draws, seed),  # type: ignore[arg-type]
            "share_negative": sum(1 for x in nets if x < 0) / len(nets) if nets else None,  # type: ignore[operator]
            "worst": min(nets) if nets else None, "crash": crash,  # type: ignore[type-var]
            "margin": {"n": len(mk), "label": margin_approx.LABEL,
                       "mean_margin": sum(r.margin for r in mk) / len(mk) if mk else None,  # type: ignore[misc]
                       "mean_net_after_carry": sum(r.net_after_carry for r in mk) / len(mk) if mk else None,  # type: ignore[misc]
                       "mean_return_on_margin": sum(r.net / r.margin for r in mk) / len(mk) if mk else None}}  # type: ignore[operator]  # fmt: skip

"""Point-in-time pricing helpers of the backtester: slippage, fills, an expiry's forward, strikes by delta and the
expiry settlement price. Every function reads one `Day` (one session's close) and nothing else.

Fills: a buy fills at the reference price + slippage, a sell at − slippage, to the paisa, never below one tick
(₹0.05). The reference is the last trade when the contract traded that day, else NSE's settlement price (a model
price, so the widest tier applies). Slippage = max(tier % × reference, Book.min_slippage) × Book.slippage_mult, with
the tier picked by contracts traded that day (specs.Book.slippage_tiers; assumptions, the bhavcopy has no bid/ask).
"""

from __future__ import annotations

import math
from datetime import date

from finresearch.backtest import costs
from finresearch.backtest.history import Day, Quote
from finresearch.backtest.specs import Book
from finresearch.fincalc import fno_history as fh
from finresearch.fincalc.options import greeks

TICK = 0.05


def slippage(ref: float, contracts: int, book: Book) -> float:
    """₹ per unit moved against the trader (see the module docstring; the one cost model is backtest.costs)."""
    return costs.slippage(ref, contracts, book)


def fill(ref: float, side: str, contracts: int, book: Book) -> float:
    s = slippage(ref, contracts, book)
    px = ref + s if side == "buy" else ref - s
    return max(TICK, round(px + 1e-9, 2))


def forward(x: Day, expiry: date, rate: float) -> float | None:
    """The expiry's forward at this close: its future's settlement price, else put-call parity on traded strikes
    (market prices only), else the underlying value × e^(rT) (fincalc.fno_history.forward_for)."""
    t = (expiry - x.day).days / 365
    if t <= 0:
        return None
    quotes = [fh.OptQuote(q.strike, q.right, q.settle) for q in x.options.values()  # type: ignore[arg-type]
              if q.expiry == expiry and q.settle and fh.market_settle(q.settle, q.close, q.contracts)]  # fmt: skip
    got = fh.forward_for(quotes, t, rate, x.futures.get(expiry), x.spot)
    return got[0] if got else None


def leg_iv(q: Quote, fwd: float, t: float, rate: float) -> float | None:
    """Black-76 IV (fraction) of the quote's reference price; None when it cannot be solved."""
    ref = q.ref
    if ref is None:
        return None
    iv = fh.black76_iv(q.right, ref, fwd, q.strike, t, rate)  # type: ignore[arg-type]
    return iv if iv is not None and 0.005 < iv < 4.99 else None


def delta(q: Quote, fwd: float, t: float, rate: float) -> float | None:
    iv = leg_iv(q, fwd, t, rate)
    if iv is None:
        return None
    return greeks(q.right, fwd, q.strike, t, rate, iv, div_yield=rate).delta  # type: ignore[arg-type]


def by_delta(x: Day, expiry: date, right: str, target: float, fwd: float, rate: float, min_contracts: int,
             otm_only: bool = True) -> Quote | None:  # fmt: skip
    """The traded strike (>= min_contracts contracts today) whose |Black-76 delta| is nearest `target`; ties go to
    the farther-from-the-money strike. OTM only unless `otm_only` is False (a debit spread's ~50-delta leg)."""
    t = (expiry - x.day).days / 365
    best: tuple[float, float, Quote] | None = None
    for q in x.chain(expiry, right):
        if q.contracts < min_contracts or not q.traded:
            continue
        if otm_only and ((right == "put" and q.strike > fwd) or (right == "call" and q.strike < fwd)):
            continue
        d = delta(q, fwd, t, rate)
        if d is None:
            continue
        key = (abs(abs(d) - target), -abs(q.strike - fwd))
        if best is None or key < best[:2]:
            best = (*key, q)
    return best[2] if best else None


def by_otm(x: Day, expiry: date, right: str, pct: float, fwd: float, min_contracts: int) -> Quote | None:
    """The traded strike (>= min_contracts contracts today) nearest the forward moved `pct` % out of the money (a put
    below, a call above; 0 = at the money); ties go to the farther-from-the-money strike (Strategy Lab, #318)."""
    target = fwd * (1 - pct / 100) if right == "put" else fwd * (1 + pct / 100)
    pool = [q for q in x.chain(expiry, right) if q.contracts >= min_contracts and q.traded]
    if not pool:
        return None
    return min(pool, key=lambda q: (abs(q.strike - target), -abs(q.strike - fwd)))


def wing(x: Day, expiry: date, right: str, anchor: float, target: float) -> Quote | None:
    """The traded strike nearest `target` that lies beyond `anchor` (lower for a put wing below, higher for a call
    wing above, as `target` says); None when no such contract traded today."""
    below = target < anchor
    pool = [
        q for q in x.chain(expiry, right) if q.traded and (q.strike < anchor if below else q.strike > anchor)
    ]
    if not pool:
        return None
    return min(pool, key=lambda q: (abs(q.strike - target), abs(q.strike - anchor)))


MAX_SETTLE_GAP = (
    1e-4  # 1 bp: the uniform settle must agree this closely with the expiring future or underlying
)


def settlement_price(x: Day, expiry: date) -> tuple[float, str] | None:
    """The final settlement price (#352; call it only on the contract's last session, which a holiday can move
    earlier). On that session every expiring option row carries one identical settle, the underlying's final
    settlement price (419 of 426 NIFTY/BANKNIFTY near expiries 2022-26, 0.0 bp from the future or underlying on all 262
    where one exists): that uniform settle is used, accepted only if it is within 1 bp of the expiring future or the
    bhavcopy's underlying value when either exists. Without option settles: the expiring future (NSE settles index and
    stock futures at the underlying's closing value), else the underlying value (UDiFF). Settles that differ with
    neither to check them, or a uniform settle that disagrees: None (unknown, never a guess; the median of K ± settle
    used before gave BANKNIFTY 13-Jan-2022 3,680.05 against 38,469.95)."""
    ref, ref_src = (x.futures[expiry], "future") if expiry in x.futures else (x.spot, "underlying")
    settles = {
        q.settle for q in x.options.values() if q.expiry == expiry and q.settle is not None and q.settle > 0
    }
    if len(settles) == 1:
        s = settles.pop()
        if ref and abs(s - ref) / ref >= MAX_SETTLE_GAP:
            return None
        return s, "uniform option settle"
    return (ref, ref_src) if ref else None


def discounted_spot(fwd: float, t: float, rate: float) -> float:
    return fwd * math.exp(-rate * t)

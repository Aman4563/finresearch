"""Forward option quotes (#363, P1.1): parsing Groww's Get Quote into the compact `option_quote` row, per-row and
per-day data-quality checks, and the interface the measured cost model (a later PR) reads.

Units in `option_quote` (compact on purpose: ~168k rows a year, issue budget ~50 MB): prices in integer paise (bid,
ask, ltp, depth prices, the underlying's LTP, the strike), quantities in units (not lots), depth as one flat integer
array per side, [price, quantity, price, quantity, ...], in the order Groww returned the levels (best first). A level
with no price is dropped; a missing or zero bid/ask is NULL with its quantity, never 0. `iv` is Groww's
implied_volatility as returned (its unit is not documented [U]; NSE rows carry NSE's %). `exchange_at` is the
exchange's last-trade time (Groww last_trade_time, epoch ms), so each quote can be aged against `requested_at`.

The cost model's maths (issue #363): mid m = (b + a)/2, half-spread h = (a - b)/2, relative half-spread h/m, and the
depth cost of q units, cost(q) = sum_j p_j x min(q_j, q_rem) - q x m for a buy over the ask levels (mirrored for a
sell over the bids). A quote without both sides, or crossed or locked, has no mid: its spread is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

# per-row data-quality flags (option_quote.flags, a bitmask)
NO_BID, NO_ASK, CROSSED, LOCKED, STALE, NO_TRADE_TIME = 1, 2, 4, 8, 16, 32
STALE_AFTER = timedelta(seconds=60)  # issue acceptance: ATM last trade within 60 s of the snapshot


def paise(v: Any) -> int | None:
    """A positive rupee price as integer paise; missing, zero, negative or NaN -> None (no price, never 0)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return int((d * 100).to_integral_value())


def _count(v: Any, *, positive: bool = False) -> int | None:
    """A whole count (OI, volume, quantity); None when missing. `positive`: zero is None too (a quantity of 0 at a
    price is no quantity)."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    n = int(v)
    return n if n > 0 or (n == 0 and not positive) else None


def _depth(levels: Any) -> list[int] | None:
    out: list[int] = []
    for lv in levels if isinstance(levels, list) else []:
        p = paise(lv.get("price")) if isinstance(lv, dict) else None
        if p is None:
            continue
        out += [p, _count(lv.get("quantity")) or 0]
    return out or None


def parse_groww(payload: dict[str, Any]) -> dict[str, Any]:
    """The option_quote columns from one Get Quote payload (adapters.groww_market docstring)."""
    bid, ask = paise(payload.get("bid_price")), paise(payload.get("offer_price"))
    depth = payload.get("depth") if isinstance(payload.get("depth"), dict) else {}
    iv = payload.get("implied_volatility")
    ltt = payload.get("last_trade_time")
    at = (datetime.fromtimestamp(ltt / 1000, UTC)
          if isinstance(ltt, (int, float)) and not isinstance(ltt, bool) and ltt > 0 else None)  # fmt: skip
    return {"bid": bid, "bid_qty": _count(payload.get("bid_quantity"), positive=True) if bid else None,
            "ask": ask, "ask_qty": _count(payload.get("offer_quantity"), positive=True) if ask else None,
            "depth_bid": _depth(depth.get("buy")), "depth_ask": _depth(depth.get("sell")),
            "ltp": paise(payload.get("last_price")), "oi": _count(payload.get("open_interest")),
            "volume": _count(payload.get("volume")),
            "iv": float(iv) if isinstance(iv, (int, float)) and not isinstance(iv, bool) and iv > 0 else None,
            "exchange_at": at}  # fmt: skip


def flags(row: dict[str, Any], requested_at: datetime) -> int:
    """The row's data-quality bitmask: a missing side, crossed (b > a) or locked (b = a), and a last trade older than
    STALE_AFTER at the request (or none at all)."""
    f = 0
    b, a = row.get("bid"), row.get("ask")
    f |= NO_BID if b is None else 0
    f |= NO_ASK if a is None else 0
    if b is not None and a is not None:
        f |= CROSSED if b > a else LOCKED if b == a else 0
    at = row.get("exchange_at")
    if at is None:
        f |= NO_TRADE_TIME
    elif requested_at - at > STALE_AFTER:
        f |= STALE
    return f


def _rupees(p: int | None) -> Decimal | None:
    return None if p is None else Decimal(p) / 100


def _levels(flat: list[int] | None) -> list[tuple[Decimal, int]]:
    flat = flat or []
    return [(Decimal(flat[i]) / 100, int(flat[i + 1])) for i in range(0, len(flat) - 1, 2)]


@dataclass(frozen=True)
class Quote:
    """One stored quote in rupees, for the cost model: mid, half-spread, relative half-spread and depth cost."""

    bid: Decimal | None
    ask: Decimal | None
    bid_qty: int | None = None
    ask_qty: int | None = None
    depth_bid: list[tuple[Decimal, int]] = field(default_factory=list)
    depth_ask: list[tuple[Decimal, int]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)  # the row's other columns (instrument, times, iv, flags)

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> Quote:
        keys = ("bid", "ask", "bid_qty", "ask_qty", "depth_bid", "depth_ask")
        return cls(bid=_rupees(r.get("bid")), ask=_rupees(r.get("ask")), bid_qty=r.get("bid_qty"),
                   ask_qty=r.get("ask_qty"), depth_bid=_levels(r.get("depth_bid")),
                   depth_ask=_levels(r.get("depth_ask")), meta={k: v for k, v in r.items() if k not in keys})  # fmt: skip

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None or self.bid >= self.ask:
            return None
        return (self.bid + self.ask) / 2

    @property
    def half_spread(self) -> Decimal | None:
        return None if self.mid is None else (self.ask - self.bid) / 2

    @property
    def rel_half_spread(self) -> Decimal | None:
        m = self.mid
        return None if m is None else self.half_spread / m

    def depth_cost(self, qty: int, side: str) -> Decimal | None:
        """Rupees paid beyond mid to buy (over the asks) or sell (over the bids) `qty` units at the shown depth; None
        when there is no mid or the shown depth is shallower than `qty`."""
        m = self.mid
        if m is None or qty <= 0:
            return None
        levels = self.depth_ask if side == "buy" else self.depth_bid
        rem, paid = qty, Decimal(0)
        for p, q in levels:
            take = min(q, rem)
            paid += p * take
            rem -= take
            if rem == 0:
                break
        if rem > 0:
            return None
        return paid - qty * m if side == "buy" else qty * m - paid

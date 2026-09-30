"""FIFO lots from a holding's transactions (pure; no database).

Indian capital-gains tax matches sales first-in-first-out within one account: CBDT Circular 768 (24-Jun-1998) for
shares in a demat account, and per folio for mutual-fund units. So a `holding` here is one instrument in one account,
and FIFO never crosses accounts.

Events (``Event.kind``):
- ``opening``: units held at the start of a statement whose cost and date are unknown (a one-year CAS). The lot keeps
  ``cost_per_unit=None`` until the user enters them; tax on it cannot be computed and says so. A statement's opening
  balance (``meta.statement_opening``) is ignored when earlier events exist: an older statement covers that history.
  A broker holdings baseline (``meta.cost_basis == "broker_average"``, finresearch.portfolio.connectors.merge) keeps
  the broker's average cost with an unknown date: P&L works, the tax term stays "unknown" until the date is entered.
- ``buy``: a purchase (also SIP, switch-in, dividend reinvestment). Cost = amount + charges (brokerage and stamp duty
  are part of the cost of acquisition; STT is not deductible under s.48 but a tradebook does not split it out, so
  whatever the user enters as charges is added).
- ``sell``: a transfer (redemption, switch-out, sale). Matched FIFO; proceeds are net of the sale's charges.
- ``remove``: units leaving without a taxable transfer (gift out, a scheme merger's switch-out, a reversal): removed
  from the newest lots for a reversal, else FIFO, without a disposal.
- ``split``: ``meta = {"from": 10, "to": 2}`` (face value). Every open lot's units × (from/to), cost per unit ÷ (from/to);
  the acquisition date is unchanged (the holding period runs from the original purchase).
- ``bonus``: ``meta = {"a": 1, "b": 1}`` (a new shares for every b held). A new lot with nil cost, acquired on the
  event's day (s.55(2)(aa)(iiia): cost of bonus shares is nil; the holding period starts at allotment. The event's
  day is the ex-date from the exchange; allotment is usually a day or two later, which rarely matters).
- ``dividend``: income only; lots are untouched.

Same-day buys and sells in one holding are intraday trades (speculative business income, not capital gains): the
netted quantity is matched buy-against-sell first and flagged ``intraday``; only the remainder goes through FIFO.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

ZERO = Decimal(0)
EPS = Decimal("0.0005")  # units below this are rounding noise (CAS units have 3 decimals)
ORDER = {"opening": 0, "split": 1, "bonus": 1, "buy": 2, "sell": 3, "remove": 3, "dividend": 4}


@dataclass
class Event:
    id: int | None
    day: date
    kind: str
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    charges: Decimal = ZERO
    stt_paid: bool = True
    meta: dict[str, Any] = field(default_factory=dict)

    def gross(self) -> Decimal | None:
        if self.amount is not None:
            return abs(self.amount)
        if self.quantity is not None and self.price is not None:
            return self.quantity * self.price
        return None


@dataclass
class Lot:
    txn_id: int | None
    acquired: date | None
    origin: str  # buy | bonus | opening
    quantity: Decimal  # split-adjusted units acquired
    open_quantity: Decimal
    cost_per_unit: Decimal | None  # incl. buy charges, split-adjusted; None = unknown
    stt_paid: bool = True

    @property
    def open_cost(self) -> Decimal | None:
        return None if self.cost_per_unit is None else self.cost_per_unit * self.open_quantity


@dataclass
class Disposal:
    txn_id: int | None
    lot: Lot | None  # None when the sale exceeded the units held
    acquired: date | None
    sold: date
    quantity: Decimal
    cost: Decimal | None
    proceeds: Decimal
    stt_paid: bool
    origin: str
    intraday: bool = False

    @property
    def gain(self) -> Decimal | None:
        return None if self.cost is None else self.proceeds - self.cost


@dataclass
class LotBook:
    lots: list[Lot]
    disposals: list[Disposal]
    dividends: list[tuple[date, Decimal]]
    warnings: list[str]

    @property
    def open_lots(self) -> list[Lot]:
        return [lot for lot in self.lots if lot.open_quantity > EPS]

    @property
    def units(self) -> Decimal:
        return sum((lot.open_quantity for lot in self.lots), ZERO)

    @property
    def open_cost(self) -> Decimal | None:
        """Cost of the open units; None if any open lot's cost is unknown."""
        costs = [lot.open_cost for lot in self.open_lots]
        return None if any(c is None for c in costs) else sum((c for c in costs if c is not None), ZERO)


def _q(x: Any) -> Decimal | None:
    return None if x is None else Decimal(str(x))


def split_factor(meta: dict[str, Any]) -> Decimal | None:
    old, new = _q(meta.get("from")), _q(meta.get("to"))
    if not old or not new or old <= 0 or new <= 0:
        return None
    return old / new


def bonus_ratio(meta: dict[str, Any]) -> tuple[Decimal, Decimal] | None:
    a, b = _q(meta.get("a")), _q(meta.get("b"))
    if not a or not b or a <= 0 or b <= 0:
        return None
    return a, b


def build_lots(events: Iterable[Event]) -> LotBook:
    """Replay a holding's events in date order and return its lots, disposals, dividends and warnings."""
    evs = sorted(events, key=lambda e: (e.day, ORDER.get(e.kind, 5), e.id or 0))
    warnings: list[str] = []
    # a statement's opening balance stands for the history before the statement. Once an earlier statement (or any
    # earlier transaction) is imported, that history is present and the opening balance would count it twice.
    if evs:
        first = evs[0].day
        kept = [
            e for e in evs if not (e.kind == "opening" and e.meta.get("statement_opening") and e.day > first)
        ]
        if len(kept) < len(evs):
            warnings.append("a statement's opening balance was ignored: earlier transactions cover it")
        evs = kept
    lots: list[Lot] = []
    disposals: list[Disposal] = []
    dividends: list[tuple[date, Decimal]] = []

    # same-day buys and sells: the netted quantity is intraday
    by_day: dict[date, dict[str, Decimal]] = {}
    for e in evs:
        if e.kind in ("buy", "sell") and e.quantity:
            by_day.setdefault(e.day, {"buy": ZERO, "sell": ZERO})[e.kind] += e.quantity
    intraday_left = {d: min(v["buy"], v["sell"]) for d, v in by_day.items() if v["buy"] > 0 and v["sell"] > 0}
    intraday_buys: dict[date, list[Lot]] = {}

    def fifo(qty: Decimal, pool: Sequence[Lot]) -> list[tuple[Lot, Decimal]]:
        taken: list[tuple[Lot, Decimal]] = []
        for lot in pool:
            if qty <= EPS:
                break
            if lot.open_quantity <= EPS:
                continue
            t = min(qty, lot.open_quantity)
            lot.open_quantity -= t
            qty -= t
            taken.append((lot, t))
        if qty > EPS:
            taken.append((None, qty))  # type: ignore[arg-type]
        return taken

    for e in evs:
        q = e.quantity if e.quantity is not None else None
        if e.kind in ("buy", "opening"):
            if not q or q <= 0:
                warnings.append(f"{e.day}: {e.kind} without units skipped")
                continue
            gross = e.gross()
            if e.kind == "opening":
                acquired = e.meta.get("acquired")
                acq = date.fromisoformat(acquired) if isinstance(acquired, str) and acquired else None
                cpu = (gross + e.charges) / q if gross is not None and e.price is not None else None
                # a broker's holdings snapshot (connectors.merge) knows the average cost but not the purchase dates:
                # the cost is kept so P&L works, the date stays unknown so tax says it cannot classify the term
                keep = acq is not None or e.meta.get("cost_basis") == "broker_average"
                lot = Lot(e.id, acq, "opening", q, q, cpu if keep else None, e.stt_paid)
            else:
                cpu = (gross + e.charges) / q if gross is not None else None
                if cpu is None:
                    warnings.append(f"{e.day}: buy of {q} units has no price or amount; its cost is unknown")
                lot = Lot(e.id, e.day, "buy", q, q, cpu, e.stt_paid)
            lots.append(lot)
            if e.day in intraday_left and e.kind == "buy":
                intraday_buys.setdefault(e.day, []).append(lot)
        elif e.kind in ("sell", "remove"):
            if not q or q <= 0:
                warnings.append(f"{e.day}: {e.kind} without units skipped")
                continue
            gross = e.gross()
            net = (gross - e.charges) if gross is not None else None
            per_unit = net / q if net is not None else None
            remaining = q
            matches: list[tuple[Lot | None, Decimal, bool]] = []
            if e.kind == "sell" and intraday_left.get(e.day, ZERO) > EPS:
                n = min(remaining, intraday_left[e.day])
                got = fifo(n, intraday_buys.get(e.day, []))
                got_q = sum((t for lot, t in got if lot is not None), ZERO)
                intraday_left[e.day] -= got_q
                remaining -= got_q
                matches += [(lot, t, True) for lot, t in got if lot is not None]
            # lots are created in date order (an opening balance first), so list order is FIFO order
            pool: Sequence[Lot] = (
                list(reversed(lots)) if e.kind == "remove" and e.meta.get("reversal") else lots
            )
            if remaining > EPS:
                matches += [(lot, t, False) for lot, t in fifo(remaining, pool)]
            if e.kind == "remove":
                if any(lot is None for lot, _, _ in matches):
                    warnings.append(f"{e.day}: removed more units than held")
                continue
            if per_unit is None:
                warnings.append(
                    f"{e.day}: sale of {q} units has no price or amount; gains cannot be computed"
                )
            for lot, t, intra in matches:
                if lot is None:
                    warnings.append(
                        f"{e.day}: sold {t.normalize()} more units than the lots hold (missing history?)"
                    )
                cost = None if lot is None or lot.cost_per_unit is None else lot.cost_per_unit * t
                disposals.append(Disposal(e.id, lot, lot.acquired if lot else None, e.day, t, cost,
                                          (per_unit * t) if per_unit is not None else ZERO,
                                          bool(e.stt_paid and (lot.stt_paid if lot else True)),
                                          lot.origin if lot else "unknown", intra))  # fmt: skip
        elif e.kind == "split":
            f = split_factor(e.meta)
            if f is None:
                warnings.append(f"{e.day}: split without a valid from/to ratio skipped")
                continue
            for lot in lots:
                if lot.open_quantity > EPS and (lot.acquired is None or lot.acquired < e.day):
                    lot.quantity *= f
                    lot.open_quantity *= f
                    if lot.cost_per_unit is not None:
                        lot.cost_per_unit /= f
        elif e.kind == "bonus":
            r = bonus_ratio(e.meta)
            if r is None:
                warnings.append(f"{e.day}: bonus without a valid a:b ratio skipped")
                continue
            held = sum(
                (lot.open_quantity for lot in lots if lot.acquired is None or lot.acquired < e.day), ZERO
            )
            new = Decimal(math.floor(held * r[0] / r[1]))  # fractional entitlements are sold by the company
            if new > 0:
                lots.append(Lot(e.id, e.day, "bonus", new, new, ZERO, True))
        elif e.kind == "dividend":
            if e.amount:
                dividends.append((e.day, abs(e.amount)))
        else:
            warnings.append(f"{e.day}: unknown event kind {e.kind!r} skipped")
    return LotBook(lots, disposals, dividends, warnings)

"""The backtester's view of the stored F&O history: one underlying-day at a time, never a later day.

`History` is what the engine reads. `DbHistory` reads the #306 tables (fno_bhav, fno_iv_eod, fno_contract) from any
database URL in READ ONLY transactions, so it can replay a copy without writing to it; `MemoryHistory` holds
synthetic days for tests. `Clocked` wraps either one for a run: the engine advances its clock to each session's close
and any request for a later day raises LookAheadError, so no decision can read a price that was not yet published.

The session calendar (`sessions`) is the one thing read ahead: knowing that tomorrow is a trading day is the
exchange's published holiday list, not market data. It is used only to exit on the last session before an expiry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

RIGHTS = {"CE": "call", "PE": "put"}
MAX_DTE_LOADED = (
    75  # calendar days: the longest expiry a spec can trade (45) plus room; farther expiries not read
)


class LookAheadError(RuntimeError):
    """A request for data after the engine's current close."""


@dataclass(frozen=True)
class Quote:
    """One option contract's end-of-day row. `close` is the last traded price (None when it did not trade);
    `settle` is NSE's settlement price (a model price when the contract did not trade)."""

    expiry: date
    right: str  # call | put
    strike: float
    close: float | None
    settle: float | None
    contracts: int  # traded that day
    oi: int
    lot: int | None = None  # UDiFF rows carry the lot size (2024 on)

    @property
    def traded(self) -> bool:
        return self.contracts > 0 and self.close is not None and self.close > 0

    @property
    def ref(self) -> float | None:
        """The day's reference price: the last trade when it traded, else NSE's settlement price."""
        if self.traded:
            return self.close
        return self.settle if self.settle and self.settle > 0 else None


@dataclass
class Day:
    day: date
    symbol: str
    options: dict[tuple[date, str, float], Quote] = field(default_factory=dict)
    futures: dict[date, float] = field(default_factory=dict)  # expiry -> settlement price
    spot: float | None = None  # the bhavcopy's underlying value (UDiFF days only)
    iv30: float | None = None  # 30-day constant-maturity ATM IV, annual %, from this day's prices only

    def expiries(self) -> list[date]:
        return sorted({k[0] for k in self.options})

    def chain(self, expiry: date, right: str) -> list[Quote]:
        return sorted((q for (e, r, _), q in self.options.items() if e == expiry and r == right),
                      key=lambda q: q.strike)  # fmt: skip

    def is_monthly(self, expiry: date) -> bool:
        """Monthly = a future with that expiry trades today (fnohist.derive's rule, applied on the day)."""
        return expiry in self.futures


class History(Protocol):
    def sessions(self, symbol: str, start: date, end: date) -> list[date]: ...

    def day(self, symbol: str, d: date) -> Day | None: ...

    def lot(self, symbol: str, expiry: date, d: date) -> int | None: ...

    def latest(self, symbol: str) -> date | None: ...

    def iv_history(self, symbol: str, start: date, end: date) -> list[float]: ...


class Clocked:
    """A History that refuses any day after the clock (see the module docstring). `max_seen` is the latest day read."""

    def __init__(self, inner: History) -> None:
        self.inner = inner
        self.clock: date | None = None
        self.max_seen: date | None = None

    def advance(self, d: date) -> None:
        self.clock = d

    def sessions(self, symbol: str, start: date, end: date) -> list[date]:
        return self.inner.sessions(symbol, start, end)

    def day(self, symbol: str, d: date) -> Day | None:
        if self.clock is None or d > self.clock:
            raise LookAheadError(f"{symbol} {d} requested at the close of {self.clock}")
        self.max_seen = d if self.max_seen is None else max(self.max_seen, d)
        return self.inner.day(symbol, d)

    def lot(self, symbol: str, expiry: date, d: date) -> int | None:
        if self.clock is None or d > self.clock:
            raise LookAheadError(f"{symbol} lot on {d} requested at the close of {self.clock}")
        return self.inner.lot(symbol, expiry, d)

    def latest(self, symbol: str) -> date | None:
        return self.inner.latest(symbol)

    def iv_history(self, symbol: str, start: date, end: date) -> list[float]:
        if self.clock is None or end > self.clock:
            raise LookAheadError(f"{symbol} IV to {end} requested at the close of {self.clock}")
        return self.inner.iv_history(symbol, start, end)

    def span_params(self, symbol: str, d: date) -> dict[str, Any] | None:
        """The NSE SPAN scan ranges known on `d` (#370) when the inner history provides them, else None."""
        if self.clock is None or d > self.clock:
            raise LookAheadError(f"{symbol} SPAN ranges on {d} requested at the close of {self.clock}")
        fn = getattr(self.inner, "span_params", None)
        return fn(symbol, d) if fn is not None else None


class MemoryHistory:
    """Synthetic days for tests: {symbol: {day: Day}} and lot sizes {(symbol, expiry): lot}."""

    def __init__(self, days: Iterable[Day], lots: dict[tuple[str, date], int] | None = None) -> None:
        self.days: dict[str, dict[date, Day]] = {}
        for x in days:
            self.days.setdefault(x.symbol, {})[x.day] = x
        self.lots = lots or {}

    def sessions(self, symbol: str, start: date, end: date) -> list[date]:
        return sorted(d for d in self.days.get(symbol, {}) if start <= d <= end)

    def day(self, symbol: str, d: date) -> Day | None:
        return self.days.get(symbol, {}).get(d)

    def lot(self, symbol: str, expiry: date, d: date) -> int | None:
        x = self.days.get(symbol, {}).get(d)
        for q in x.options.values() if x else ():
            if q.expiry == expiry and q.lot:
                return q.lot
        return self.lots.get((symbol, expiry))

    def latest(self, symbol: str) -> date | None:
        return max(self.days.get(symbol, {}), default=None)

    def iv_history(self, symbol: str, start: date, end: date) -> list[float]:
        return [
            x.iv30
            for d, x in sorted(self.days.get(symbol, {}).items())
            if start <= d <= end and x.iv30 is not None
        ]


def within(d: date, expiry: date) -> bool:
    return d <= expiry <= d + timedelta(days=MAX_DTE_LOADED)

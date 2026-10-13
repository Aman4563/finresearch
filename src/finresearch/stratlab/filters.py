"""The Strategy Lab's entry filters (#318): weekday, IV percentile / rank range and trend, as an engine `gate`.

Point in time: the gate sees only the closes the engine has read (`on_day` at each session, warm-up included) and
today's IV statistics. Anything it cannot read is unknown and refuses the entry, counted by reason; it never passes.

The trend filter's price is the underlying's level at each close: the nearest future's settlement price discounted
to today at the book's rate (F·e^(−r·t), t = days to that future's expiry ÷ 365), else the bhavcopy's underlying
value (UDiFF days). The future is used first so the whole history (2022 on, before UDiFF carried the underlying) is
read the same way. A day without either is unknown, and an average over a window holding an unknown day is unknown.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from finresearch.backtest.history import Day
from finresearch.fincalc.volatility import IvStats
from finresearch.stratlab.spec import WEEKDAYS, LabSpec

TREND_N = {"above_ma50": 50, "above_ma200": 200, "below_ma50": 50, "below_ma200": 200}


def underlying_price(x: Day, rate: float) -> float | None:
    live = sorted(e for e in x.futures if e >= x.day)
    if live:
        e = live[0]
        return x.futures[e] * math.exp(-rate * (e - x.day).days / 365)
    return x.spot if x.spot and x.spot > 0 else None


class Filters:
    """One run's gate. Call `on_day` for every session read (engine.run does) before the entry decision of that day."""

    def __init__(self, spec: LabSpec, rate: float) -> None:
        self.spec, self.rate = spec, rate
        self.closes: list[tuple[date, float | None]] = []

    def on_day(self, x: Day) -> None:
        self.closes.append((x.day, underlying_price(x, self.rate)))

    def moving_average(self, n: int) -> float | None:
        last = [v for _, v in self.closes[-n:]]
        if len(last) < n or any(v is None for v in last):
            return None
        return sum(last) / n  # type: ignore[arg-type]

    def gate(self, _st: object, x: Day, stats: IvStats | None) -> str | None:
        s = self.spec
        if x.day.weekday() not in s.weekdays:
            return f"weekday filter ({WEEKDAYS[x.day.weekday()] if x.day.weekday() < 5 else 'weekend'})"
        if s.iv_min is not None or s.iv_max is not None:
            v = None if stats is None else (stats.percentile if s.iv_measure == "percentile" else stats.rank)
            if v is None:
                return f"IV {s.iv_measure} unknown"
            v *= 100
            if s.iv_min is not None and v < s.iv_min:
                return f"IV {s.iv_measure} below the filter"
            if s.iv_max is not None and v > s.iv_max:
                return f"IV {s.iv_measure} above the filter"
        n = TREND_N.get(s.trend)
        if n:
            ma = self.moving_average(n)
            price = self.closes[-1][1] if self.closes and self.closes[-1][0] == x.day else None
            if ma is None or price is None:
                return f"trend unknown (fewer than {n} sessions with a price)"
            above = price > ma
            if s.trend.startswith("above") and not above:
                return f"trend filter: price not above its {n}-session average"
            if s.trend.startswith("below") and above:
                return f"trend filter: price not below its {n}-session average"
        return None


def price_series(
    history: Any, symbol: str, start: date, end: date, rate: float
) -> list[tuple[date, float | None]]:
    """[(session, underlying_price)] from `start` to `end`, oldest first, for the paper rule's trend filter: one SQL
    read of the futures (and UDiFF underlying value) on a DbHistory, else every stored day."""
    from finresearch.backtest.dbhistory import DbHistory

    if not isinstance(history, DbHistory):
        out = []
        for d in history.sessions(symbol, start, end):
            x = history.day(symbol, d)
            out.append((d, underlying_price(x, rate) if x is not None else None))
        return out
    from sqlalchemy import select

    from finresearch.db.models import FnoBhav

    days: dict[date, Day] = {}
    with history._s() as s:
        rows = s.execute(select(FnoBhav.day, FnoBhav.instrument, FnoBhav.expiry, FnoBhav.settle, FnoBhav.underlying)
                         .where(FnoBhav.symbol == symbol, FnoBhav.day.between(start, end))
                         .where(FnoBhav.instrument.in_(("IDF", "STF")))).all()  # fmt: skip
        sessions = history.sessions(symbol, start, end)
    for d, _kind, e, settle, und in rows:
        x = days.setdefault(d, Day(d, symbol))
        if settle is not None and settle > 0:
            x.futures[e] = float(settle)
        if und is not None and und > 0:
            x.spot = float(und)
    return [(d, underlying_price(days[d], rate) if d in days else None) for d in sessions]

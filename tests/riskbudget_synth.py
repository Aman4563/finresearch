"""Synthetic NIFTY and BANKNIFTY history for the risk-budget replay's tests (#395 Stage 2): narrow Black-76 chains
(tests/fno_synth.py) so the tests stay fast, an optional futures-only warm-up so the registered beta can become known,
and BANKNIFTY moving exactly 1.2 x NIFTY's log return. Made-up prices only."""

from __future__ import annotations

import math
from datetime import date

import fno_synth as fs

from finresearch.backtest.history import Day, MemoryHistory

NIFTY_LOT, BANK_LOT, BANK_BETA = 75, 30, 1.2


def bank_path(nifty: dict[date, float], s0: float = 45000.0) -> dict[date, float]:
    n0 = nifty[min(nifty)]
    return {d: s0 * (s / n0) ** BANK_BETA for d, s in nifty.items()}


def futures_only(sym: str, d: date, s: float, monthly: set[date]) -> Day:
    x = Day(d, sym, spot=s, iv30=None)
    for e in sorted(monthly):
        if 0 <= (e - d).days <= 75:
            x.futures[e] = round(s * math.exp(fs.RATE * (e - d).days / 365), 2)
    return x


def history(start: date, end: date, *, chains_from: date | None = None, prices: dict[date, float] | None = None,
            bank: bool = True, seed: int = 7, iv30: dict[date, float] | None = None) -> MemoryHistory:  # fmt: skip
    """Sessions from `start` to `end`; before `chains_from` only the monthly futures are stored. `iv30`: each
    session's 30-day IV reading (the IV filter's input; options stay priced at the flat volatility)."""
    px = prices or fs.path(start, end, s0=20000.0, vol=0.15, seed=seed)
    exps, monthly = fs.expiries(start, end)
    bpx = bank_path(px)
    days = []
    for d in sorted(px):
        if chains_from is not None and d < chains_from:
            days.append(futures_only("NIFTY", d, px[d], monthly))
            if bank:
                days.append(futures_only("BANKNIFTY", d, bpx[d], monthly))
            continue
        iv = -1.0 if iv30 is None else iv30[d]
        days.append(
            fs.day("NIFTY", d, px[d], exps, monthly, 0.15, NIFTY_LOT, step=100.0, width=0.08, iv30=iv)
        )
        if bank:
            days.append(fs.day("BANKNIFTY", d, bpx[d], sorted(monthly), monthly, 0.18, BANK_LOT, step=500.0,
                               width=0.10, iv30=iv))  # fmt: skip
    return MemoryHistory(days)

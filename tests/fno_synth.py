"""Synthetic F&O history for the backtester's tests (#307): a made-up index whose options are priced with Black-76
at one flat volatility on a seeded geometric Brownian path. No network, no database, no real prices."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from finresearch.backtest.history import Day, MemoryHistory, Quote
from finresearch.fincalc.fno_history import black76_price

RATE = 0.065


def last_thursday(y: int, m: int) -> date:
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 3) % 7)


def expiries(start: date, end: date) -> tuple[list[date], set[date]]:
    """Every Thursday is an expiry; the last Thursday of a month is the monthly one (it has a future)."""
    d = start - timedelta(days=(start.weekday() - 3) % 7)
    out = []
    while d <= end + timedelta(days=80):
        out.append(d)
        d += timedelta(days=7)
    return out, {last_thursday(e.year, e.month) for e in out}


def path(start: date, end: date, s0: float = 20000.0, vol: float = 0.15, seed: int = 7) -> dict[date, float]:
    rnd = random.Random(seed)
    out, s, d = {}, s0, start
    while d <= end:
        if d.weekday() < 5:
            out[d] = s
            s *= math.exp(-0.5 * vol * vol / 252 + vol / math.sqrt(252) * rnd.gauss(0, 1))
        d += timedelta(days=1)
    return out


def day(sym: str, d: date, s: float, exps: list[date], monthly: set[date], vol: float, lot: int | None,
        step: float = 50.0, width: float = 0.12, iv30: float | None = -1.0) -> Day:  # fmt: skip
    x = Day(d, sym, spot=s, iv30=vol * 100 if iv30 == -1.0 else iv30)
    for e in exps:
        dte = (e - d).days
        if dte < 0 or dte > 75:
            continue
        t = dte / 365
        f = s * math.exp(RATE * t)
        if e in monthly:
            x.futures[e] = round(f, 2)
        k = math.floor(s * (1 - width) / step) * step
        while k <= s * (1 + width):
            for right in ("call", "put"):
                if dte == 0:
                    p = max(0.0, s - k) if right == "call" else max(0.0, k - s)
                else:
                    p = black76_price(right, f, k, t, RATE, vol)
                p = round(p, 2)
                if p >= 0.05:
                    n = max(1, int(3000 * math.exp(-(((k - s) / s / 0.04) ** 2))))
                    x.options[(e, right, k)] = Quote(e, right, k, p, p, n, 10 * n * (lot or 50), lot)
            k += step
    return x


def history(start: date, end: date, *, sym: str = "NIFTY", vol: float = 0.15, seed: int = 7, lot: int | None = 50,
            prices: dict[date, float] | None = None, iv30: float | None = -1.0) -> MemoryHistory:  # fmt: skip
    px = prices or path(start, end, vol=vol, seed=seed)
    exps, monthly = expiries(start, end)
    return MemoryHistory([day(sym, d, s, exps, monthly, vol, lot, iv30=iv30) for d, s in sorted(px.items())])

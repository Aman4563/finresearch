"""Generated price series for the stock factor diagnostics' tests (#506): made-up names and closes, never market data.
`cache` writes them in the layout stock_backtest.load reads (prices/<symbol>.csv and an index file)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from finresearch.evals.stock_backtest import Series
from finresearch.factordiag.panel import Month, Panel
from finresearch.fincalc.signals import Adjusted

MARKET = "NIFTYBEES"  # the calendar series' file name in the cache layout; its closes here are generated too


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def series(
    symbol: str, days: Sequence[date], closes: Sequence[float], anomalies: Iterable[date] = ()
) -> Series:
    c = [float(x) for x in closes]
    return Series(symbol, Adjusted(list(days), c, list(c), list(c), [], list(anomalies)))


def walk(
    rng: np.random.Generator, n: int, *, vol: float = 0.015, drift: float = 0.0, start: float = 100.0
) -> list[float]:
    """A geometric random walk of n closes with daily log-return N(drift, vol)."""
    return (start * np.exp(np.cumsum(rng.normal(drift, vol, n)))).tolist()


def names(n: int) -> list[str]:
    return [f"SYN{i:02d}" for i in range(n)]


def universe(rng: np.random.Generator, days: Sequence[date], symbols: Sequence[str], *,
             drift: Callable[[int], float] = lambda i: 0.0) -> dict[str, Series]:  # fmt: skip
    """One random walk per made-up symbol; `drift(i)` is symbol i's daily drift (0: pure noise)."""
    return {s: series(s, days, walk(rng, len(days), drift=drift(i))) for i, s in enumerate(symbols)}


def cache(data_dir: Path, uni: dict[str, Series], market: Series) -> None:
    """Write the series as the price cache stock_backtest.load reads."""
    (data_dir / "prices").mkdir(parents=True)
    for s in (*uni.values(), market):
        rows = "".join(
            f"{d.isoformat()},{c!r},{c!r},{c!r}\n" for d, c in zip(s.adj.days, s.adj.close, strict=True)
        )
        (data_dir / "prices" / f"{s.symbol}.csv").write_text("date,close,high,low\n" + rows)
    (data_dir / "index_NIFTY50.csv").write_text("date,close\n")  # read by load(), not used by the diagnostics


def month_end_dates(n: int, start_year: int = 2010) -> list[date]:
    """n consecutive calendar month-ends."""
    out = []
    for k in range(n):
        y, m = start_year + (k + 1) // 12, (k + 1) % 12 + 1
        out.append(date(y, m, 1) - timedelta(days=1))
    return out


def array_panel(scores: dict[str, np.ndarray], monthly: np.ndarray, windows: Sequence[tuple[int, int]],
                dates: Sequence[date] | None = None) -> Panel:  # fmt: skip
    """A panel straight from arrays: scores[component][t, i] is name i's score at month t, monthly[u, i] its return
    over month u (the month that ENDS at decision date u), so the return over (a, b] from t is the sum of
    monthly[t + a + 1 .. t + b]. A NaN score or return is a missing name. Months whose window ends beyond the array
    are not evaluated."""
    t_n, n = monthly.shape
    syms = names(n)
    dates = list(dates) if dates is not None else month_end_dates(t_n)
    months = []
    for t in range(t_n):
        sc = {
            c: {syms[i]: float(v[t, i]) for i in range(n) if not np.isnan(v[t, i])} for c, v in scores.items()
        }
        month = Month(dates[t], n, len(next(iter(sc.values()))), sc)
        for a, b in windows:
            if t + b >= t_n:
                continue
            r = monthly[t + a + 1 : t + b + 1].sum(axis=0)
            month.returns[a, b] = {syms[i]: float(r[i]) for i in range(n) if not np.isnan(r[i])}
            month.flags[a, b] = {}
        months.append(month)
    return Panel(months, {}, {})

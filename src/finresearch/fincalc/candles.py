"""Intraday candles from a 1-minute price series.

NSE's public intraday series (the quote page's 1D chart) is one last-traded-price sample per minute, stamped hh:mm:59,
plus a trailing live point; it carries no volume and no per-minute open/high/low. Candles are therefore "candles of
1-minute closes": for each bucket, open = the first sample, high/low = the highest/lowest sample, close = the last
sample. Moves inside a minute are not seen, so highs and lows can be understated versus exchange tick candles.

Buckets are aligned to the 09:15 IST open (5m: 09:15, 09:20, ...; 1h: 09:15, 10:15, ... 15:15), the convention of
Indian broker charts (Kite Connect offers minute, 3/5/10/15/30/60-minute and day intervals). Pre-open samples
(phase "PO", 09:00-09:08) are left out; so are samples after the 15:30 close.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
INTERVALS: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


@dataclass(frozen=True)
class Tick:
    at: datetime  # IST, timezone-aware
    price: Decimal
    phase: str = "NM"  # "NM" normal market, "PO" pre-open
    volume: Decimal | None = (
        None  # shares traded in the minute ending at `at` (BSE's series has it; NSE's has not)
    )


@dataclass(frozen=True)
class Candle:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    samples: int
    partial: bool = False  # still filling: the session is open and the bucket ends after the newest sample
    volume: Decimal | None = None  # sum of the samples' volumes; None when the series has no volume


def interval_minutes(interval: str) -> int:
    try:
        return INTERVALS[interval]
    except KeyError:
        raise ValueError(f"unknown interval {interval!r}; use one of {', '.join(INTERVALS)}") from None


def normalise(ticks: list[Tick]) -> list[Tick]:
    """Sorted by time, one tick per timestamp (the last one wins: NSE repeats rows)."""
    by_at: dict[datetime, Tick] = {}
    for t in ticks:
        by_at[t.at] = t
    return [by_at[k] for k in sorted(by_at)]


def session_ticks(ticks: list[Tick]) -> list[Tick]:
    """Normal-market samples between 09:15:00 and 15:30:59 (the close's own sample is kept)."""
    close_edge = time(SESSION_CLOSE.hour, SESSION_CLOSE.minute, 59)
    return [t for t in normalise(ticks) if t.phase != "PO" and SESSION_OPEN <= t.at.time() <= close_edge]


def _session_start(d: date, tz) -> datetime:
    return datetime.combine(d, SESSION_OPEN, tzinfo=tz)


def aggregate(ticks: list[Tick], interval: str, *, live: bool = False) -> list[Candle]:
    """Candles of `interval` ("1m", "5m", "15m", "30m", "1h") for one or more sessions, oldest first.

    A sample stamped 09:19:59 is the price at the end of minute 09:19, so it closes the 09:15 5-minute bucket; the
    close's 15:30 sample goes into the last bucket of the day. With `live`, the newest bucket is marked partial until
    its closing sample (stamped one second before its end) has arrived."""
    n = interval_minutes(interval)
    ticks = session_ticks(ticks)
    if not ticks:
        return []
    groups: dict[datetime, list[Tick]] = {}
    for t in ticks:
        open_at = _session_start(t.at.date(), t.at.tzinfo)
        close_at = datetime.combine(t.at.date(), SESSION_CLOSE, tzinfo=t.at.tzinfo)
        minute = min(t.at.replace(second=0, microsecond=0), close_at - timedelta(minutes=1))
        k = int((minute - open_at).total_seconds() // 60) // n
        groups.setdefault(open_at + timedelta(minutes=k * n), []).append(t)
    out: list[Candle] = []
    for start in sorted(groups):
        g = groups[start]
        close_at = datetime.combine(start.date(), SESSION_CLOSE, tzinfo=start.tzinfo)
        end = min(start + timedelta(minutes=n), close_at)
        prices = [t.price for t in g]
        vols = [t.volume for t in g if t.volume is not None]
        out.append(Candle(start, end, prices[0], max(prices), min(prices), prices[-1], len(g),
                          volume=sum(vols, Decimal(0)) if vols else None))  # fmt: skip
    if live:
        last, newest = out[-1], ticks[-1].at
        if newest < last.end - timedelta(seconds=1):  # a bucket's closing sample is stamped end - 1 s
            out[-1] = dataclasses.replace(last, partial=True)
    return out

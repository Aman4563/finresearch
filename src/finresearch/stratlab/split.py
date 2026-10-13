"""The in-sample / out-of-sample split of a Strategy Lab strategy (#318), locked when the strategy is created.

Rule (stated before any lab result was seen):
- When the stored history of the underlying starts by the pre-registered library's in-sample start (+ 31 days) and
  reaches at least 180 days past its out-of-sample start, the library's windows are used (in-sample 2022-01-01 to
  2024-06-30, out of sample from 2024-07-01), so a lab strategy is judged on the same days as the library.
- Otherwise the stored span is split 70 / 30 by calendar days: out of sample from the first day of the month after
  the 70 % point. A span shorter than MIN_SPAN_DAYS is refused (too little history for an honest split).
The split is stored on the strategy and never recomputed: every later version is judged on the same windows, and
out of sample always runs to the latest stored day. Re-creating the strategy gives the same split for the same
history, so it cannot be used to move the out-of-sample window either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from finresearch.backtest import specs

MIN_SPAN_DAYS = 540  # about 18 months: a year in sample (IV percentile needs 60 days of warm-up) and 5+ months out
LIB_START_SLACK = timedelta(days=31)
LIB_OOS_MIN = timedelta(days=180)


class SplitError(ValueError):
    pass


@dataclass(frozen=True)
class Split:
    is_start: date
    is_end: date
    oos_start: date
    data_first: date
    data_last: date
    rule: str


def lock(first: date | None, last: date | None) -> Split:
    if first is None or last is None:
        raise SplitError("no stored F&O history for this underlying")
    if (last - first).days < MIN_SPAN_DAYS:
        raise SplitError(f"only {(last - first).days} days of stored history ({first} to {last}); a strategy needs "
                         f"at least {MIN_SPAN_DAYS} for an in-sample / out-of-sample split")  # fmt: skip
    if first <= specs.IS_START + LIB_START_SLACK and last >= specs.OOS_START + LIB_OOS_MIN:
        return Split(max(first, specs.IS_START), specs.IS_END, specs.OOS_START, first, last,
                     "the pre-registered library's windows (same days as the library)")  # fmt: skip
    cut = first + timedelta(days=round((last - first).days * 0.7))
    oos = date(cut.year + (cut.month == 12), cut.month % 12 + 1, 1)
    return Split(first, oos - timedelta(days=1), oos, first, last,
                 f"70 / 30 of the stored span {first} to {last} (out of sample from the month after the 70 % point)")  # fmt: skip


def span(history: Any, symbol: str) -> tuple[date | None, date | None]:
    """(first, last) stored day of `symbol`: SQL min/max on a DbHistory, else the sessions of an in-memory history."""
    from finresearch.backtest.dbhistory import DbHistory

    if isinstance(history, DbHistory):
        from sqlalchemy import func, select

        from finresearch.db.models import FnoBhav

        with history._s() as s:
            row = s.execute(select(func.min(FnoBhav.day), func.max(FnoBhav.day)).where(FnoBhav.symbol == symbol)).one()
        return row[0], row[1]
    days = history.sessions(symbol, date.min, date.max)
    return (days[0], days[-1]) if days else (None, None)

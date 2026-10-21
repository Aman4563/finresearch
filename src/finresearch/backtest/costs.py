"""One cost model for the backtest and the paper book (#382 (d), #363 P1.1). Charges are `fincalc.charges` (dated)
for both; this module is the other half, slippage, so the two records are priced alike.

- Tiers (the registered model, specs.Book.slippage_tiers [U: assumptions; the bhavcopy has no bid/ask]): a fraction
  of the reference price picked by contracts traded that day, at least Book.min_slippage, × Book.slippage_mult. The
  backtest (pricing.fill) and the trade plan's half-spread fallback (signals.fno) read them here; an unknown volume
  takes the widest tier, never the narrowest.
- Measured (#363): the median relative half-spread h/m (h = (ask − bid)/2, m = the mid) of the clean quotes the
  forward recorder stored (fnohist.quotes; rows without both sides, crossed or locked are left out). It needs
  MIN_MEASURED_SESSIONS recorded sessions within LOOKBACK_DAYS (the issue's fit window is the first 40 sessions);
  with fewer it is unknown, never a guess. It sets the paper book's default slippage (`paper_default`); the paper
  fill is the live LTP moved by that %, the user can still change it per trade.
- Not built yet: #363's fitted quantile model mapped onto history (it needs 60 sessions of quotes and the
  pre-registered adoption test). Until it is adopted the backtest keeps the tiers; a measured cost model is a NEW
  cost variant of the library, the registered verdicts stay.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Any

from finresearch.backtest.specs import BOOK, Book

MIN_MEASURED_SESSIONS = 40
LOOKBACK_DAYS = 120  # calendar days of recorded quotes the measured median reads (about 80 sessions)
ASSUMED_PAPER_SLIPPAGE_PCT = 0.5
ASSUMED_SOURCE = (
    f"assumption: {ASSUMED_PAPER_SLIPPAGE_PCT:g} % of the LTP (fewer than {MIN_MEASURED_SESSIONS} sessions of "
    "recorded quotes to measure the spread)"
)


def tier_fraction(contracts: float | None, book: Book = BOOK) -> float:
    """The slippage tier for `contracts` traded that day; None (unknown) takes the widest tier."""
    if contracts is None:
        return book.slippage_tiers[-1][1]
    return next((p for n, p in book.slippage_tiers if contracts >= n), book.slippage_tiers[-1][1])


def slippage(ref: float, contracts: float | None, book: Book = BOOK) -> float:
    """₹ per unit moved against the trader: max(tier × reference, the minimum) × the multiplier."""
    return max(tier_fraction(contracts, book) * ref, book.min_slippage) * book.slippage_mult


@dataclass(frozen=True)
class Measured:
    median_pct: float  # the median of h/m, in % of the mid
    rows: int
    sessions: int
    first: date
    last: date

    def label(self) -> str:
        return (f"measured: median half-spread of {self.rows:,} recorded quotes over {self.sessions} sessions "
                f"({self.first:%d-%b-%Y} to {self.last:%d-%b-%Y})")  # fmt: skip


def measured_half_spread(rows: Iterable[tuple[date, float]],
                         min_sessions: int = MIN_MEASURED_SESSIONS) -> Measured | None:  # fmt: skip
    """The median relative half-spread of (session, h/m) values; None with fewer than `min_sessions` sessions."""
    vals = [(d, r) for d, r in rows if r is not None and r >= 0]
    days = {d for d, _ in vals}
    if len(days) < min_sessions:
        return None
    return Measured(median([r for _, r in vals]) * 100, len(vals), len(days), min(days), max(days))


def measured_from_quotes(s: Any, today: date) -> Measured | None:
    """`measured_half_spread` over the recorded option quotes of the LOOKBACK_DAYS up to `today` (IST days)."""
    from sqlalchemy import select

    from finresearch.db.models import OptionQuote as Q
    from finresearch.fincalc.dates import IST, to_ist
    from finresearch.fnohist import quotes as fq

    start = datetime.combine(today - timedelta(days=LOOKBACK_DAYS), time(0, 0), IST)
    end = datetime.combine(today + timedelta(days=1), time(0, 0), IST)
    bad = fq.NO_BID | fq.NO_ASK | fq.CROSSED | fq.LOCKED
    got = s.execute(select(Q.snapshot_at, Q.bid, Q.ask).where(Q.snapshot_at >= start, Q.snapshot_at < end,
                                                               Q.flags.op("&")(bad) == 0, Q.bid.is_not(None),
                                                               Q.ask.is_not(None), Q.ask > Q.bid))  # fmt: skip
    return measured_half_spread(
        (to_ist(at).date(), (a - b) / (a + b)) for at, b, a in got
    )  # h/m = ((a-b)/2) / ((a+b)/2)


def paper_default(s: Any, today: date) -> tuple[float, str]:
    """The paper book's default slippage (% of the LTP) and where it comes from: the measured median half-spread when
    enough sessions are recorded, else the labelled assumption."""
    m = measured_from_quotes(s, today)
    if m is None:
        return ASSUMED_PAPER_SLIPPAGE_PCT, ASSUMED_SOURCE
    return round(m.median_pct, 3), m.label()

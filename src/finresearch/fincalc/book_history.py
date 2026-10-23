"""History applied to today's F&O book (#394): the scan over every stored start day with its 99th-percentile loss,
expected shortfall (ES97.5) and 10 worst dates; the multi-week episodes as daily paths; and the gap and expiry-day
scenarios. Analysis only: nothing here places, modifies or cancels an order. Pure: the caller loads the series
(fnohist.book_history) and prices the book (fincalc.book_scenarios).

Historical-scenario revaluation over every start day s (#394's "Maths"): the book is revalued h sessions from now with
each underlying's forward moved by its own stored move S_{s+h}/S_s - 1 (front-future log returns within one contract,
fincalc.fno_history.front_future_returns, compounded) and every leg's IV by its own IV30_{s+h} - IV30_s (sticky
strike: the skew change is not applied). An underlying without its own stored move in a window moves by the worse end
of its beta band x NIFTY's move, with NIFTY's IV30 change, and is counted. A window with a missing NIFTY return (or
one spanning two sessions) or a missing IV30 is not a scenario: it is skipped and counted, never 0.

The ES is a historical-simulation ES97.5 (equal weights, fincalc.taildist.expected_shortfall) of these P&Ls: an
estimate of the tail from the stored history (2022 on), not P3.4's model ES, which waits for a validated physical
model (#387). Every output says which.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc.taildist import ES_ALPHA, expected_shortfall

MIN_SCAN_DAYS = (
    250  # [U] fewer start days: the 99th percentile and ES97.5 are unknown (6.25 tail observations)
)
WORST_N = 10  # the 10 worst dates (#394)
LOSS_Q = 0.01  # the 99th-percentile loss: the 1 % quantile of P&L, inverted CDF (an observed P&L, no interpolation)
ES_NOTE = (f"Historical-simulation ES{100 * (1 - ES_ALPHA):g}: the mean of the worst {ES_ALPHA:.1%} of the scan's "
           "P&Ls (equal weights) on the stored history, not a model ES (P3.4, #387, waits for a validated model).")  # fmt: skip


@dataclass(frozen=True)
class Series:
    """One underlying's stored history: front-future log returns by session d1 -> (previous session d0, r), and IV30
    by day in vol points."""

    returns: Mapping[date, tuple[date, float]]
    iv30: Mapping[date, float]


def calendar(s: Series) -> list[date]:
    """The sessions of a series: every day with a return or an IV30."""
    return sorted(set(s.iv30) | set(s.returns) | {d0 for d0, _ in s.returns.values()})


def window(s: Series, cal: Sequence[date], i: int, h: int) -> tuple[float, float] | None:
    """(relative move, IV30 change in points) from cal[i] to cal[i + h]; None when a return in it is missing or spans
    two sessions of `cal`, or either IV30 is missing."""
    if i + h >= len(cal):
        return None
    tot = 0.0
    for k in range(i + 1, i + h + 1):
        got = s.returns.get(cal[k])
        if got is None or got[0] != cal[k - 1]:
            return None
        tot += got[1]
    a, b = s.iv30.get(cal[i]), s.iv30.get(cal[i + h])
    if a is None or b is None:
        return None
    return math.exp(tot) - 1, b - a


def _moves(hist: Mapping[str, Series], unders: Sequence[str], cal: Sequence[date], i: int,
           h: int) -> tuple[tuple[float, float] | None, dict[str, tuple[float, float]]]:  # fmt: skip
    """NIFTY's window and each other underlying's own window where it has one."""
    nifty = window(hist["NIFTY"], cal, i, h)
    own = {}
    for u in unders:
        if u != "NIFTY" and u in hist:
            # the underlying's own sessions must match NIFTY's: its window is read on NIFTY's calendar
            w = window(hist[u], cal, i, h)
            if w is not None:
                own[u] = w
    return nifty, own


@dataclass(frozen=True)
class ScanDay:
    start: date
    end: date
    move: float  # NIFTY's move
    dvol: float  # NIFTY's IV30 change
    pnl: float
    mapped: tuple[str, ...] = ()  # underlyings moved by beta x NIFTY in this window (no own stored move)


@dataclass(frozen=True)
class Scan:
    """The scan at horizon h: every complete start day's P&L, the windows skipped (a missing NIFTY return or IV30),
    and over them the 99th-percentile loss, ES97.5 and the 10 worst (positive numbers are losses). `reasons`: why the
    whole scan is unknown (an unpriced leg, an unknown beta, no stored NIFTY series); `es_reasons`: why the tail
    figures are unknown although the days are known (too few)."""

    h: int
    days: tuple[ScanDay, ...]
    skipped: int
    loss_99: float | None
    es: float | None
    worst: tuple[ScanDay, ...]
    reasons: tuple[str, ...] = ()
    es_reasons: tuple[str, ...] = ()
    mapped: Mapping[str, int] = field(default_factory=dict)
    flags: tuple[str, ...] = ()

    @property
    def first(self) -> date | None:
        return self.days[0].start if self.days else None

    @property
    def last(self) -> date | None:
        return self.days[-1].start if self.days else None


def _unknown(h: int, reasons: Sequence[str], skipped: int = 0) -> Scan:
    return Scan(h, (), skipped, None, None, (), tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(reasons)))


def tail(pnls: Sequence[float]) -> tuple[float, float]:
    """(99th-percentile loss, ES97.5), both positive losses, of equally weighted P&Ls."""
    x = np.asarray(pnls, float)
    return -float(np.quantile(x, LOSS_Q, method="inverted_cdf")), expected_shortfall(x)


def scan(legs: Sequence[bs.BookLeg], m: bs.Market, hist: Mapping[str, Series], h: int,
         holidays: frozenset[date] | set[date] = frozenset(), min_days: int = MIN_SCAN_DAYS) -> Scan:  # fmt: skip
    """Today's book revalued h sessions ahead under every stored start day's moves (module docstring)."""
    states = bs.price_legs(legs, m)
    bad = [s.reason for s in states if s.reason]
    if bad:
        return _unknown(h, bad)
    if "NIFTY" not in hist:
        return _unknown(
            h, ["NIFTY's front-future and IV30 history is not stored, so no historical scenario exists"]
        )
    unders = sorted({x.underlying.upper() for x in legs})
    cal, at = calendar(hist["NIFTY"]), bs.horizon_clock(m.now, h, holidays)
    days: list[ScanDay] = []
    skipped, mapped = 0, dict.fromkeys([u for u in unders if u != "NIFTY"], 0)
    for i in range(len(cal) - h):
        nifty, own = _moves(hist, unders, cal, i, h)
        if nifty is None:
            skipped += 1
            continue
        c = bs._revalue(states, m, nifty[0], nifty[1], at, f"{h}d", False, {**own, "NIFTY": nifty})
        if c.pnl is None:
            return _unknown(h, c.reasons, skipped)
        miss = tuple(u for u in unders if u != "NIFTY" and u not in own)
        for u in miss:
            mapped[u] += 1
        days.append(ScanDay(cal[i], cal[i + h], nifty[0], nifty[1], c.pnl, miss))
    flags = tuple(f for s in states for f in s.flags)
    if len(days) < min_days:
        why = (f"{len(days)} complete start days at h = {h}, fewer than the {min_days} needed for a 99th percentile "
               f"and ES{100 * (1 - ES_ALPHA):g}")  # fmt: skip
        return Scan(h, tuple(days), skipped, None, None, (), (), (why,), mapped, flags)
    q99, es = tail([d.pnl for d in days])
    worst = tuple(sorted(days, key=lambda d: (d.pnl, d.start))[:WORST_N])
    return Scan(h, tuple(days), skipped, q99, es, worst, (), (), mapped, flags)

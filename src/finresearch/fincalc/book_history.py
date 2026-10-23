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
from datetime import date, datetime

import numpy as np

from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc import risk_alerts as ra
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


# --------------------------------------------------------------------------- multi-week episodes (#394)
EPISODE_H = 21  # sessions (#394: multi-week episodes are replayed as daily paths at h = 21)


@dataclass(frozen=True)
class Episode:
    key: str
    label: str
    start: date  # the window: its last stored session before `start` is the first possible path start
    end: date
    source: str
    waits: str | None = None  # why it cannot be replayed yet


VAULT_WAIT = ("waits for the vault's data audit (#394, #365): 2008-2021 sessions live only in the sealed vault "
              "database, never in the app's store")  # fmt: skip
EPISODES = (
    Episode("adani_2023", "Adani sell-off, Jan-Feb 2023", date(2023, 1, 25), date(2023, 2, 28),
            "#394's library: the Adani episode of Jan-Feb 2023, from the report of 24-Jan-2023 (after the close)"),
    Episode("febmar_2026", "Feb-Mar 2026 fall", date(2026, 2, 1), date(2026, 3, 31),
            "#394: NIFTY future -14.1 %, BANKNIFTY -17.3 %, IV30 9.7 to 29.5 (the window of "
            "portfolio.stress_replay and fincalc.risk_alerts.EPISODES)"),
    Episode("oct_2008", "October 2008", date(2008, 10, 1), date(2008, 10, 31), "#394 (public crisis days)", VAULT_WAIT),
    Episode("mar_2020", "March 2020", date(2020, 3, 1), date(2020, 3, 31), "#394 (public crisis days)", VAULT_WAIT),
)  # fmt: skip


@dataclass(frozen=True)
class PathPoint:
    day: date  # the historical session
    d: int  # sessions since the path's start (the book is revalued d sessions from now)
    move: float  # NIFTY's cumulative move
    dvol: float  # NIFTY's IV30 change since the start
    pnl: float
    intrinsic: int = 0  # legs valued at intrinsic (expired inside d sessions)
    mapped: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpisodeResult:
    episode: Episode
    start: date | None
    path: tuple[PathPoint, ...]
    worst: PathPoint | None
    candidates: int = 0  # complete path starts compared
    reasons: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()


def _path(states: Sequence[bs.LegState], m: bs.Market, hist: Mapping[str, Series], unders: Sequence[str],
          cal: Sequence[date], i: int, n: int, holidays: frozenset[date] | set[date]) -> list[PathPoint] | tuple[str, ...] | None:  # fmt: skip
    out = []
    for d in range(1, n + 1):
        nifty, own = _moves(hist, unders, cal, i, d)
        if nifty is None:
            return None
        c = bs._revalue(states, m, nifty[0], nifty[1], bs.horizon_clock(m.now, d, holidays), f"{d}d", False,
                        {**own, "NIFTY": nifty})  # fmt: skip
        if c.pnl is None:
            return c.reasons
        miss = tuple(u for u in unders if u != "NIFTY" and u not in own)
        out.append(PathPoint(cal[i + d], d, nifty[0], nifty[1], c.pnl, c.intrinsic, miss))
    return out


def episode(legs: Sequence[bs.BookLeg], m: bs.Market, hist: Mapping[str, Series], ep: Episode,
            holidays: frozenset[date] | set[date] = frozenset(), h: int = EPISODE_H) -> EpisodeResult:  # fmt: skip
    """The episode as daily paths of up to h sessions applied to today's book (moves as in `scan`), from each start
    between the last session before the window and the window, staying inside it; the path whose worst point is the
    lowest is shown (ties: the earliest start). A leg that expires inside the path is valued at intrinsic."""
    if ep.waits:
        return EpisodeResult(ep, None, (), None, 0, (f"{ep.label} {ep.waits}",))
    states = bs.price_legs(legs, m)
    bad = [s.reason for s in states if s.reason]
    if bad:
        return EpisodeResult(ep, None, (), None, 0, tuple(dict.fromkeys(bad)))
    cal = calendar(hist["NIFTY"]) if "NIFTY" in hist else []
    first = max((i for i, d in enumerate(cal) if d < ep.start), default=None)
    inside = [i for i, d in enumerate(cal) if ep.start <= d <= ep.end]
    if first is None or not inside or cal[-1] < ep.end:
        span = f"{cal[0]} to {cal[-1]}" if cal else "nothing"
        why = f"{ep.label}: the stored NIFTY series ({span}) does not cover {ep.start} to {ep.end}"
        return EpisodeResult(ep, None, (), None, 0, (why,))
    last = inside[-1]
    starts = [i for i in range(first, last) if i + h <= last] or [first]
    unders = sorted({x.underlying.upper() for x in legs})
    best: tuple[float, int, list[PathPoint]] | None = None
    n_ok = 0
    for i in starts:
        p = _path(states, m, hist, unders, cal, i, min(h, last - i), holidays)
        if isinstance(p, tuple):
            return EpisodeResult(ep, None, (), None, 0, tuple(dict.fromkeys(p)))
        if p:
            n_ok += 1
            w = min(x.pnl for x in p)
            if best is None or w < best[0]:
                best = (w, i, p)
    flags = tuple(f for s in states for f in s.flags)
    if best is None:
        why = f"{ep.label}: a NIFTY return or IV30 is missing on every path inside the window"
        return EpisodeResult(ep, None, (), None, 0, (why,), flags)
    path = tuple(best[2])
    worst = min(path, key=lambda x: (x.pnl, x.d))
    return EpisodeResult(ep, cal[best[1]], path, worst, n_ok, (), flags)


# --------------------------------------------------------------------------- gap and expiry-day scenarios [U]
# #394 lists them without maths; the definitions here are this module's (labelled [U] in the API and the PR):
# - gap: NIFTY's stored overnight gaps of the front future (ln(open / the same contract's previous close),
#   fincalc.risk_alerts.front_future), applied at once (no time passes, so no theta is earned) with the spot-vol
#   response; other underlyings by the worse end of their beta band.
# - expiry day: NIFTY's stored front-future close-to-close returns on the contract's own expiry day, applied at the
#   close of the book's nearest expiry (the underlying is assumed unchanged until that morning) with the response;
#   legs expiring then are at intrinsic.
MIN_GAPS = ra.MIN_GAPS  # about a year of stored gaps before the distribution is read (the risk alerts' rule)
MIN_EXPIRY_DAYS = 30  # [U] stored expiry days before their distribution is read
POINTS = (("worst fall", None), ("1st percentile", 0.01), ("99th percentile", 0.99), ("worst rise", None))


@dataclass(frozen=True)
class Shock:
    kind: str  # gap | expiry_day
    label: str
    move: float | None  # NIFTY's relative move
    at: datetime  # the revaluation clock
    pnl: float | None
    reasons: tuple[str, ...] = ()
    n: int = 0  # stored observations the point was read from


def _points(logs: Sequence[float]) -> list[tuple[str, float]]:
    x = np.asarray(sorted(logs), float)
    out = []
    for label, q in POINTS:
        v = (
            x[0]
            if label == "worst fall"
            else x[-1]
            if label == "worst rise"
            else np.quantile(x, q, method="inverted_cdf")
        )
        out.append((label, math.exp(float(v)) - 1))
    return out


def _shocks(kind: str, legs: Sequence[bs.BookLeg], m: bs.Market, logs: Sequence[float], at: datetime,
            need: int, what: str) -> list[Shock]:  # fmt: skip
    if len(logs) < need:
        why = f"{len(logs)} stored {what}, fewer than the {need} needed"
        return [Shock(kind, label, None, at, None, (why,), len(logs)) for label, _ in POINTS]
    states = bs.price_legs(legs, m)
    out = []
    for label, mv in _points(logs):
        c = bs._revalue(states, m, mv, 0.0, at, kind, True)
        out.append(Shock(kind, label, mv, at, c.pnl, c.reasons, len(logs)))
    return out


def gap_scenarios(legs: Sequence[bs.BookLeg], m: bs.Market, gaps: Sequence[float],
                  min_n: int = MIN_GAPS) -> list[Shock]:  # fmt: skip
    """The book repriced now at NIFTY's worst, 1st- and 99th-percentile and best stored overnight gaps."""
    return _shocks("gap", legs, m, gaps, m.now, min_n, "NIFTY overnight gaps")


def expiry_day_scenarios(legs: Sequence[bs.BookLeg], m: bs.Market, rets: Sequence[float],
                         min_n: int = MIN_EXPIRY_DAYS) -> list[Shock]:  # fmt: skip
    """The book at the close of its nearest expiry under NIFTY's stored expiry-day moves (worst, 1 %, 99 %, best)."""
    from finresearch.fincalc.dates import EXPIRY_CLOSE, ist_datetime

    if not legs:
        return []
    at = ist_datetime(min(x.expiry for x in legs), *EXPIRY_CLOSE)
    return _shocks("expiry_day", legs, m, rets, at, min_n, "NIFTY expiry-day moves")

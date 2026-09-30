"""Stock-signal arithmetic (docs/dev/RESEARCH_ROADMAP.md §D.2 and §D.6, item 13): split/bonus adjustment, trend,
momentum, realised volatility, ATR, 52-week position, valuation percentile against the stock's own history, the
Wilson interval, and volatility-scaled sizing with a quarter-Kelly ceiling.

These are statistical estimates, so this module works in floats (the rest of fincalc keeps Decimal for money).
Price series are oldest first. Returns are fractions.

Evidence behind the rules (roadmap §D.2, §F):
- Momentum and value premia are supported in India, size is not: IIMA Indian Fama-French-Momentum data library
  (Agarwalla, Jacob & Varma; 1994-2014 WML ~21.9 %/yr, HML ~15.3 %/yr, SMB ~0) [39][40].
- Time-series momentum / trend ("12-month excess return > 0", "price above its 10-month average"): Moskowitz,
  Ooi & Pedersen 2012 [41]; Faber 2007 [42]. India-specific 200-DMA evidence was not found [W].
- Volatility scaling: Moreira & Muir 2017 [43]; Harvey et al. 2018 [44]. Kelly is dominated by estimation error, so
  a quarter-Kelly is shown as a CEILING only: MacLean, Thorp & Ziemba 2010 [45].
- ATR stops are risk control, not alpha: Kaminski & Lo 2014 [46].
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from finresearch.fincalc.dates import add_years

TRADING_DAYS = 252
SKIP_DAYS = 21  # the "-1" in 12-1 momentum: skip the latest month (short-term reversal)


# --------------------------------------------------------------------------- corporate-action adjustment
_BONUS = re.compile(r"\bbonus\b[^0-9]*(\d+)\s*:\s*(\d+)", re.I)
_SPLIT = re.compile(r"(?:split|sub[- ]?division|splt).*?(?:from|frm)\s*(?:rs|re)?\.?\s*(\d+(?:\.\d+)?)"
                    r".*?to\s*(?:rs|re)?\.?\s*(\d+(?:\.\d+)?)", re.I | re.S)  # fmt: skip


def action_factor(subject: str) -> float | None:
    """How many shares one old share became: "Bonus 1:1" -> 2 (a:b = a new for every b held -> (a+b)/b);
    "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share" -> 5; a subject carrying both
    ("Bonus 1:1/Face Value Split ... From Rs 10/- ... To Rs 2/-") -> 10. None for anything else."""
    f = 1.0
    if m := _BONUS.search(subject):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            f *= (a + b) / b
    if m := _SPLIT.search(subject):
        old, new = float(m.group(1)), float(m.group(2))
        if new > 0 and old > new:
            f *= old / new
    return f if f != 1.0 else None


@dataclass
class Adjusted:
    days: list[date]
    close: list[float]
    high: list[float | None]
    low: list[float | None]
    applied: list[tuple[date, float]]  # (ex-date, factor) actually applied
    anomalies: list[date]  # one-day moves beyond ±35 % that no split or bonus explains


def adjust_for_actions(days: Sequence[date], close: Sequence[float], actions: Sequence[tuple[date, str]], *,
                       high: Sequence[float | None] | None = None, low: Sequence[float | None] | None = None,
                       jump: float = 0.35) -> Adjusted:  # fmt: skip
    """Back-adjust unadjusted NSE closes for splits and bonuses (NSE's history API does not adjust them).

    A split/bonus is applied only when the price actually dropped by about its factor on (or within three trading
    days after) the ex-date: this protects against actions whose ex-date is misstated or already reflected. A one-day
    move beyond ±`jump` that no action explains is reported in `anomalies` so callers can exclude that window."""
    n = len(close)
    hi = list(high) if high is not None else [None] * n
    lo = list(low) if low is not None else [None] * n
    factors = [1.0] * n  # multiplier for each day's price
    applied: list[tuple[date, float]] = []
    explained: set[int] = set()
    by_day: dict[date, float] = {}  # a split and a bonus on the same ex-date compound
    for ex, subject in {
        (ex, " ".join(subj.lower().split())) for ex, subj in actions
    }:  # NSE repeats some rows
        f = action_factor(subject)
        if f and ex is not None:
            by_day[ex] = by_day.get(ex, 1.0) * f
    for ex, f in sorted(by_day.items()):
        i = bisect_right(list(days), ex) - 1  # index of the ex-date (or the last day before it)
        if i < 0 or days[i] != ex:
            i += 1
        for j in range(max(1, i), min(n, i + 4)):
            r = close[j] / close[j - 1]
            if abs(r * f - 1) < 0.25:  # the observed drop matches the factor
                for k in range(j):
                    factors[k] /= f
                applied.append((days[j], f))
                explained.add(j)
                break
    adj = [c * fct for c, fct in zip(close, factors, strict=True)]
    anomalies = [days[j] for j in range(1, n) if j not in explained and abs(adj[j] / adj[j - 1] - 1) > jump]
    return Adjusted(list(days), adj, [h * f if h is not None else None for h, f in zip(hi, factors, strict=True)],
                    [x * f if x is not None else None for x, f in zip(lo, factors, strict=True)], applied, anomalies)  # fmt: skip


# --------------------------------------------------------------------------- trend, momentum, volatility
def sma(close: Sequence[float], window: int) -> float | None:
    if window < 1:
        raise ValueError("window must be >= 1")
    return sum(close[-window:]) / window if len(close) >= window else None


def momentum_12_1(
    close: Sequence[float], lookback: int = TRADING_DAYS, skip: int = SKIP_DAYS
) -> float | None:
    """Return from 12 months ago to 1 month ago: close[t-21] / close[t-252] - 1 (trading days)."""
    if len(close) <= lookback:
        return None
    return close[-1 - skip] / close[-1 - lookback] - 1


def realised_vol(close: Sequence[float], window: int = TRADING_DAYS) -> float | None:
    """Annualised sample standard deviation of the last `window` daily simple returns (x sqrt 252)."""
    if len(close) < window + 1:
        return None
    c = close[-window - 1 :]
    r = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(TRADING_DAYS)


def atr(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int = 14
) -> float | None:
    """Wilder's average true range: TR = max(H-L, |H-C_prev|, |L-C_prev|); the first ATR is the mean of the first
    `period` TRs, then ATR_t = (ATR_{t-1} (period-1) + TR_t) / period."""
    if len(close) < period + 1:
        return None
    tr = [
        max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        for i in range(1, len(close))
    ]
    a = sum(tr[:period]) / period
    for x in tr[period:]:
        a = (a * (period - 1) + x) / period
    return a


def range_position(price: float, low: float, high: float) -> float | None:
    """Where the price sits in [low, high]: 0 at the low, 1 at the high."""
    return None if high <= low else (price - low) / (high - low)


def percentile_rank(values: Sequence[float], x: float) -> float | None:
    """Share of `values` at or below x (0..1)."""
    return sum(1 for v in values if v <= x) / len(values) if values else None


# --------------------------------------------------------------------------- valuation against own history
def ttm_eps_series(quarters: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Trailing-twelve-month EPS known on each date: `quarters` are (date the result became public, quarterly EPS),
    oldest first; each entry from the fourth on is the sum of the latest four."""
    q = sorted(quarters)
    return [(q[i][0], sum(e for _, e in q[i - 3 : i + 1])) for i in range(3, len(q))]


@dataclass(frozen=True)
class FiledPeriod:
    """One period's EPS as filed: a quarter, a half-year (a half-yearly filer's March filing reports Oct-Mar), a
    year-to-date (H1 in the September filing, nine months in December's) or a fiscal year. `known` is the day it
    became public; `consolidated` the filing's basis (None when unknown)."""

    start: date
    end: date
    eps: float
    known: date
    consolidated: bool | None = None

    @property
    def months(self) -> int:
        return round(((self.end - self.start).days + 1) / 30.44)


@dataclass(frozen=True)
class TtmEps:
    """A trailing-twelve-month EPS ending on `end`, known from `known`, summed from `pieces` (oldest first)."""

    known: date
    end: date
    eps: float
    basis: str  # "TTM from four quarters", "TTM from two half-years", "last fiscal year EPS", ...
    pieces: tuple[FiledPeriod, ...]


_PIECE = {3: "a quarter", 6: "a half-year", 9: "nine months", 12: "a year"}
TTM_TOLERANCE_DAYS = 3  # adjacent periods may be a day or two apart in filings; a year is 365 or 366 days


def ttm_basis(pieces: Sequence[FiledPeriod]) -> str:
    """How a trailing-twelve-month EPS was built, in words."""
    m = [p.months for p in pieces]
    if len(m) == 1:
        return "last fiscal year EPS"
    if m == [3, 3, 3, 3]:
        return "TTM from four quarters"
    if m == [6, 6]:
        return "TTM from two half-years"
    return "TTM from " + " + ".join(_PIECE.get(x, f"{x} months") for x in m)


def _is_year(start: date, end: date, tol: int = TTM_TOLERANCE_DAYS) -> bool:
    return 365 - tol <= (end - start).days + 1 <= 366 + tol


def ttm_chains(
    periods: Sequence[FiledPeriod], end: date, max_pieces: int = 4
) -> list[tuple[FiledPeriod, ...]]:
    """Every way to tile the twelve months ending on `end` with filed periods that do not overlap: the last piece ends
    on `end`, each earlier piece ends the day before the next starts (± a few days), and together they span one year.
    Pieces are returned oldest first."""
    tol = TTM_TOLERANCE_DAYS
    out: list[tuple[FiledPeriod, ...]] = []

    def walk(to: date, chain: tuple[FiledPeriod, ...]) -> None:
        for p in periods:
            if p in chain or abs((p.end - to).days) > tol or p.start > p.end:
                continue
            got = (p, *chain)
            if _is_year(p.start, end):
                out.append(got)
            elif (end - p.start).days + 1 < 365 - tol and len(got) < max_pieces:
                walk(p.start - timedelta(days=1), got)

    walk(end, ())
    return out


def ttm_from_periods(periods: Sequence[FiledPeriod], max_pieces: int = 4) -> list[TtmEps]:
    """Trailing-twelve-month EPS known on each date, from whatever the company files (roadmap §D.2 valuation factor).

    Quarterly filers give four quarters; a half-yearly filer (its March filing reports Oct-Mar, e.g. ASM Technologies,
    BSE 526433) gives two half-years (H1 from the September filing's year-to-date + H2 from March's), or a half-year
    and two quarters; any fiscal year's own EPS is the TTM at the year end. For each period end the preferred tiling is
    the fiscal year itself, then four quarters, then the fewest pieces, always on one basis when one exists (consolidated
    before standalone, as the results route ranks filings). EPS of different periods is added as filed (the usual approximation: share counts shift a little
    between periods). Rows are ordered by the day they became known, each ending later than the one before (a newer
    period end supersedes an older one; an older end learned later never replaces a newer one)."""
    uniq: dict[tuple[date, date, bool | None], FiledPeriod] = {}
    for p in sorted(periods, key=lambda x: x.known):
        uniq.setdefault(
            (p.start, p.end, p.consolidated), p
        )  # a restated period keeps its first publication date
    ps = list(uniq.values())
    rows: list[TtmEps] = []
    for end in sorted({p.end for p in ps}):
        chains = ttm_chains(ps, end, max_pieces)
        if not chains:
            continue

        def rank(c: tuple[FiledPeriod, ...]) -> tuple[int, int, int, int, date]:
            uniform = len({p.consolidated for p in c}) == 1
            style = 0 if len(c) == 1 else 1 if [p.months for p in c] == [3, 3, 3, 3] else 2
            consolidated = all(p.consolidated is True for p in c)
            return (0 if uniform else 1, style, len(c), 0 if consolidated else 1, max(p.known for p in c))

        best = min(chains, key=rank)
        rows.append(TtmEps(max(p.known for p in best), end, sum(p.eps for p in best), ttm_basis(best), best))
    out: list[TtmEps] = []
    for r in sorted(rows, key=lambda r: (r.known, r.end)):
        if out and r.end <= out[-1].end:
            continue
        if out and r.known == out[-1].known:
            out[-1] = r
        else:
            out.append(r)
    return out


def ttm_gaps(periods: Sequence[FiledPeriod], end: date) -> list[tuple[date, date]]:
    """The spans of the twelve months ending on `end` that no filed period lying wholly inside them covers: what is
    missing before a TTM EPS can be built for that end (e.g. Jul-Sep 2025 for a half-yearly filer at June 2026 whose
    September filing is not on file)."""
    start = add_years(end, -1) + timedelta(days=1)
    inside = sorted((p.start, p.end) for p in periods
                    if p.start >= start - timedelta(days=TTM_TOLERANCE_DAYS) and p.end <= end)  # fmt: skip
    gaps: list[tuple[date, date]] = []
    cur = start
    for a, b in inside:
        if (a - cur).days > TTM_TOLERANCE_DAYS:
            gaps.append((cur, a - timedelta(days=1)))
        cur = max(cur, b + timedelta(days=1))
    if (end - cur).days >= TTM_TOLERANCE_DAYS:
        gaps.append((cur, end))
    return gaps


def pe_series(days: Sequence[date], close: Sequence[float], ttm: Sequence[tuple[date, float]]) -> list[float]:
    """Daily P/E = close / the TTM EPS known that day; days with no TTM EPS yet, or a non-positive one, are skipped."""
    out: list[float] = []
    known = [d for d, _ in ttm]
    for d, c in zip(days, close, strict=True):
        i = bisect_right(known, d) - 1
        if i >= 0 and ttm[i][1] > 0:
            out.append(c / ttm[i][1])
    return out


# --------------------------------------------------------------------------- uncertainty and sizing
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for k successes in n trials (roadmap §C.10): centre (p + z²/2n)/(1 + z²/n),
    half-width z sqrt(p(1-p)/n + z²/4n²)/(1 + z²/n). 7/10 -> about 0.40-0.89."""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def kelly_fraction(mean_excess: float, vol: float) -> float | None:
    """Continuous-time Kelly f* = mu / sigma² (both annual). Dominated by estimation error: show a quarter of it as
    a ceiling only (MacLean, Thorp & Ziemba 2010)."""
    return None if vol <= 0 else mean_excess / (vol * vol)


def vol_scaled_weight(vol: float, risk_budget: float) -> float | None:
    """Weight whose standalone annual volatility equals `risk_budget` of the portfolio: w = budget / sigma."""
    return None if vol <= 0 else risk_budget / vol


def position_size(
    vol: float | None, *, risk_budget: float, cap: float, mean_excess: float | None = None
) -> dict:
    """The suggested weight: volatility-scaled, then capped by the profile's single-stock limit, then by a
    quarter-Kelly ceiling (never raised by it). Returns the weight and each step, as fractions of the portfolio."""
    if vol is None or vol <= 0:
        return {"weight": None, "reason": "no volatility estimate"}
    w = vol_scaled_weight(vol, risk_budget) or 0.0
    steps = {"vol_scaled": w, "profile_cap": cap}
    binding = "volatility"
    if w > cap:
        w, binding = cap, "profile cap"
    if mean_excess is not None:
        k = kelly_fraction(mean_excess, vol)
        qk = max(0.0, k / 4) if k is not None else None
        steps["quarter_kelly"] = qk  # type: ignore[assignment]
        if qk is not None and w > qk:
            w, binding = qk, "quarter-Kelly ceiling"
    return {"weight": w, "binding": binding, **steps}

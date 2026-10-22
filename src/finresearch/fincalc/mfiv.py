"""Model-free implied variance (P3.6, #389): the Cboe VIX / India VIX single-term variance and its 30-day combination.

    sigma^2 = (2/T) sum_i (dK_i / K_i^2) e^(RT) Q(K_i) - (1/T)(F/K0 - 1)^2
    dK_i = (K_(i+1) - K_(i-1)) / 2, and at the strip's two ends the distance to the one neighbour
    sigma^2_30 = [T1 s1^2 (N2 - N30)/(N2 - N1) + T2 s2^2 (N30 - N1)/(N2 - N1)] x N365 / N30   (N in minutes)

Sources: Cboe, "Volatility Index Methodology: Cboe Volatility Index" v6.0 (26-Feb-2026), section 3 and Appendix 3
(https://cdn.cboe.com/api/global/us_indices/governance/Volatility_Index_Methodology_Cboe_Volatility_Index.pdf);
NSE, "India VIX" white paper (2014; https://www.nseindia.com/content/indices/white_paper_IndiaVIX.pdf), sections
3-6. Both were fetched 22-Oct-2026 and their worked examples are tests (tests/test_mfiv.py). The formula is the
discretised log-contract replication of Demeterfi, Derman, Kamal and Zou (1999); Bakshi, Kapadia and Madan's (2003)
spanning gives the same second moment (fincalc/smile.bkm_moments integrates it over a fitted smile instead).

Choices, as the two papers make them:
- Forward F: given by the caller (India VIX: the same-expiry future), else Cboe's parity rule at the strike with the
  smallest |C - P|: F = K + e^(RT)(C - P).
- K0: the listed strike equal to or immediately below F (Cboe). Q(K0) = the mean of the call and put prices; both
  must have a market price, else the term is unknown.
- Strip: puts below K0 walking down, calls above K0 walking up, skipping a strike with no market price and stopping
  at the second CONSECUTIVE strike without one (Cboe's zero-bid rule; the bhavcopy has no bids, so the caller's
  "market price" is fincalc.fno_history.market_settle). India VIX's cubic-spline fill of missing quotes is not
  applied (it needs the order book's spreads).
- Too sparse = unknown with the reason, never a number: fewer than MIN_PER_SIDE strikes on either side, or two
  neighbouring strip strikes further apart than MAX_GAP of F.
- `band`: the common moneyness range |ln K/F| <= band (vault vrp.md). The strip must reach both ends of it (else
  unknown) and is then cut to it, so eras are compared over the same range.
- `extend_flat`: the single-day figure of #389, the strip continued beyond its ends at the edge strike's own Black-76
  volatility, with the edge spacing, until the price is negligible. A sensitivity; never the registered primary.
Pure functions, no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from finresearch.fincalc import black76 as b76

MIN_PER_SIDE = 3  # India VIX's minimum of 3 OTM/ATM knot points per month (white paper 4.2.8)
MAX_GAP = (
    0.05  # a hole wider than 5 % of F between neighbouring strip strikes: the term is unknown [U: choice]
)
MINUTES_DAY, N30, N365 = 1440, 43_200, 525_600
EXT_NEGLIGIBLE = 1e-10  # the flat extension stops once a strike's price is below this fraction of F ...
EXT_MAX_STRIKES = 100_000  # ... or after this many added strikes per side


@dataclass(frozen=True)
class Strike:
    """One listed strike of one expiry: a market price for the call and the put, or None (no market price)."""

    strike: float
    call: float | None
    put: float | None


@dataclass(frozen=True)
class Term:
    value: float | None  # sigma^2, annualised on the caller's T
    reason: str | None
    t: float
    forward: float | None = None
    k0: float | None = None
    n_put: int = 0
    n_call: int = 0
    k_lo: float | None = None  # the strip's lowest and highest strike
    k_hi: float | None = None
    strip: tuple[tuple[float, float], ...] = field(default=(), repr=False)  # (K, Q) used


def _priced(x: float | None) -> bool:
    return x is not None and math.isfinite(x) and x > 0


def parity_forward(strikes: Sequence[Strike], t: float, rate: float) -> float | None:
    both = [s for s in strikes if _priced(s.call) and _priced(s.put)]
    if not both:
        return None
    s = min(both, key=lambda x: (abs(x.call - x.put), x.strike))  # type: ignore[operator]
    return s.strike + math.exp(rate * t) * (s.call - s.put)  # type: ignore[operator]


def _walk(rows: Sequence[Strike], side: str) -> list[tuple[float, float]]:
    out, misses = [], 0
    for s in rows:
        q = getattr(s, side)
        if not _priced(q):
            misses += 1
            if misses == 2:
                break
            continue
        misses = 0
        out.append((s.strike, q))
    return out


def _sum(strip: Sequence[tuple[float, float]], t: float, rate: float) -> float:
    ks = [k for k, _ in strip]
    tot = 0.0
    for i, (k, q) in enumerate(strip):
        dk = ks[1] - ks[0] if i == 0 else ks[-1] - ks[-2] if i == len(ks) - 1 else (ks[i + 1] - ks[i - 1]) / 2
        tot += dk / (k * k) * q
    return tot * math.exp(rate * t)


def _extend(
    strip: list[tuple[float, float]], fwd: float, t: float, rate: float
) -> list[tuple[float, float]] | None:
    """The strip continued beyond both ends at the edge strike's own volatility (None if an edge IV is unknown)."""
    df = math.exp(-rate * t)
    (k_lo, q_lo), (k_hi, q_hi) = strip[0], strip[-1]
    lo_iv = b76.implied_vol("put", q_lo, fwd, k_lo, t, df).sigma
    hi_iv = b76.implied_vol("call", q_hi, fwd, k_hi, t, df).sigma
    if lo_iv is None or hi_iv is None:
        return None
    dlo, dhi = strip[1][0] - k_lo, k_hi - strip[-2][0]
    low: list[tuple[float, float]] = []
    k = k_lo - dlo
    while k > 0 and len(low) < EXT_MAX_STRIKES:
        q = b76.price("put", fwd, k, t, df, lo_iv)
        if q < EXT_NEGLIGIBLE * fwd:
            break
        low.append((k, q))
        k -= dlo
    high: list[tuple[float, float]] = []
    k = k_hi + dhi
    while len(high) < EXT_MAX_STRIKES:
        q = b76.price("call", fwd, k, t, df, hi_iv)
        if q < EXT_NEGLIGIBLE * fwd:
            break
        high.append((k, q))
        k += dhi
    return [*reversed(low), *strip, *high]


def term_variance(strikes: Sequence[Strike], t: float, rate: float, forward: float | None = None, *,
                  band: float | None = None, extend_flat: bool = False, min_per_side: int = MIN_PER_SIDE,
                  max_gap: float = MAX_GAP) -> Term:  # fmt: skip
    """One expiry's model-free variance (module docstring), or unknown with the reason."""
    if not (t > 0 and math.isfinite(t)):
        return Term(None, "no time to expiry", t)
    rows = sorted(strikes, key=lambda s: s.strike)
    if not rows:
        return Term(None, "no strikes", t)
    fwd = forward if forward is not None and forward > 0 else parity_forward(rows, t, rate)
    if fwd is None:
        return Term(None, "no forward: no future given and no strike with both a call and a put price", t)
    below = [s for s in rows if s.strike <= fwd]
    if not below:
        return Term(None, f"no listed strike at or below the forward {fwd:g}", t, fwd)
    at = below[-1]
    k0 = at.strike
    if not (_priced(at.call) and _priced(at.put)):
        return Term(None, f"K0 = {k0:g} lacks a market price for the call or the put", t, fwd, k0)
    puts = _walk([s for s in reversed(rows) if s.strike < k0], "put")[::-1]
    calls = _walk([s for s in rows if s.strike > k0], "call")
    strip = [*puts, (k0, (at.call + at.put) / 2), *calls]  # type: ignore[operator]
    if band is not None:
        lo, hi = fwd * math.exp(-band), fwd * math.exp(band)
        if strip[0][0] > lo * (1 + 1e-12) or strip[-1][0] < hi * (1 - 1e-12):
            return Term(None, f"the traded strikes {strip[0][0]:g}-{strip[-1][0]:g} do not cover the common range "
                        f"|ln K/F| <= {band:g} ({lo:.6g}-{hi:.6g})", t, fwd, k0, len(puts), len(calls),
                        strip[0][0], strip[-1][0])  # fmt: skip
        strip = [(k, q) for k, q in strip if lo * (1 - 1e-12) <= k <= hi * (1 + 1e-12)]
    n_put = sum(1 for k, _ in strip if k < k0)
    n_call = sum(1 for k, _ in strip if k > k0)
    base = dict(t=t, forward=fwd, k0=k0, n_put=n_put, n_call=n_call, k_lo=strip[0][0], k_hi=strip[-1][0],
                strip=tuple(strip))  # fmt: skip
    if min(n_put, n_call) < min_per_side:
        return Term(None, f"too sparse: {n_put} puts and {n_call} calls with a market price (needs "
                    f"{min_per_side} per side)", **base)  # type: ignore[arg-type]  # fmt: skip
    gap = max(b[0] - a[0] for a, b in pairwise(strip))
    if gap > max_gap * fwd:
        return Term(None, f"too sparse: a gap of {gap:g} between strip strikes is above {max_gap:.0%} of the "
                    f"forward", **base)  # type: ignore[arg-type]  # fmt: skip
    if extend_flat:
        ext = _extend(strip, fwd, t, rate)
        if ext is None:
            return Term(None, "the flat extension needs both edge strikes' Black-76 volatility", **base)  # type: ignore[arg-type]
        strip = ext
    v = 2 / t * _sum(strip, t, rate) - (fwd / k0 - 1) ** 2 / t
    if not (v > 0 and math.isfinite(v)):
        return Term(None, f"non-positive variance {v:g}", **base)  # type: ignore[arg-type]
    return Term(v, None, **base)  # type: ignore[arg-type]


def eod_minutes(days: int) -> int:
    """Minutes from one session's close (15:30 IST) to the close of the expiry `days` calendar days later: India
    VIX's T = M_current day + M_settlement day + M_other days with the computation at the close."""
    return days * MINUTES_DAY


def thirty_day(v1: float | None, n1: float, v2: float | None, n2: float, n30: float = N30,
               n365: float = N365) -> float | None:  # fmt: skip
    """The 30-day variance from two terms' variances and minutes to expiry (module docstring). Linear in total
    variance through both points: when both expiries are beyond 30 days the far weight is negative (extrapolation),
    as the formula gives. None when an input is unknown, n1 >= n2, or the result is not positive."""
    if v1 is None or v2 is None or not 0 < n1 < n2:
        return None
    t1, t2 = n1 / n365, n2 / n365
    v = (t1 * v1 * (n2 - n30) / (n2 - n1) + t2 * v2 * (n30 - n1) / (n2 - n1)) * n365 / n30
    return v if v > 0 and math.isfinite(v) else None

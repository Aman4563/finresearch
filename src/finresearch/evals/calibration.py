"""Calibration metrics for probability forecasts (docs/dev/RESEARCH_ROADMAP.md §C.10, Step 3).

Pure functions, no I/O. `p` is the forecast probability of the event and `o` the outcome (1 = it happened, 0 = not).
Every function returns None instead of NaN/inf when a metric is undefined, so the results serialise to JSON.

* Brier score (Brier 1950): BS = (1/N) Σ (pᵢ − oᵢ)². 0 is perfect; always forecasting 0.5 scores 0.25.
* Reference (climatology) Brier: BS_ref = ō(1 − ō), the score of always forecasting the observed base rate ō.
* Brier skill score: BSS = 1 − BS / BS_ref. Positive = better than the base rate; undefined when BS_ref = 0.
* Log loss: −(1/N) Σ [oᵢ ln pᵢ + (1 − oᵢ) ln(1 − pᵢ)], with p clipped to [ε, 1 − ε].
* Wilson score interval (Wilson 1927) for a proportion k/n:
  centre (p̂ + z²/2n) / (1 + z²/n), half-width z √(p̂(1 − p̂)/n + z²/4n²) / (1 + z²/n). 7/10 → about 40–89 %.
* Reliability diagram: about five equal-count bins, each with its mean forecast, observed frequency and Wilson CI
  (Bröcker & Smith 2007 on binning; roadmap source [30]).
* Hit rate: the share of directional calls (p ≠ 0.5) that pointed the right way, with a Wilson CI.

Recalibration (Platt / isotonic) is deliberately absent: the roadmap says not to fit anything below ~50–100
resolved cases, and one investor produces tens a year.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

Z95 = 1.959963984540054  # two-sided 95 % normal quantile
EPS = 1e-6


def _check(p: Sequence[float], o: Sequence[int]) -> None:
    if len(p) != len(o):
        raise ValueError(f"{len(p)} probabilities but {len(o)} outcomes")
    for x in p:
        if not 0.0 <= x <= 1.0:
            raise ValueError(f"probability {x} outside [0, 1]")
    for y in o:
        if y not in (0, 1):
            raise ValueError(f"outcome {y} is not 0 or 1")


def brier(p: Sequence[float], o: Sequence[int]) -> float | None:
    _check(p, o)
    if not p:
        return None
    return sum((pi - oi) ** 2 for pi, oi in zip(p, o, strict=True)) / len(p)


def base_rate(o: Sequence[int]) -> float | None:
    return sum(o) / len(o) if o else None


def brier_reference(o: Sequence[int]) -> float | None:
    """ō(1 − ō): the Brier score of always forecasting the sample's own base rate."""
    b = base_rate(o)
    return None if b is None else b * (1 - b)


def brier_skill(p: Sequence[float], o: Sequence[int]) -> float | None:
    bs, ref = brier(p, o), brier_reference(o)
    if bs is None or ref is None or ref == 0:
        return None
    return 1 - bs / ref


def log_loss(p: Sequence[float], o: Sequence[int], eps: float = EPS) -> float | None:
    _check(p, o)
    if not p:
        return None
    total = 0.0
    for pi, oi in zip(p, o, strict=True):
        q = min(1 - eps, max(eps, pi))
        total += -(oi * math.log(q) + (1 - oi) * math.log(1 - q))
    return total / len(p)


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for k successes in n trials; None when n = 0."""
    if n <= 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"k={k} outside 0..{n}")
    ph = k / n
    denom = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / denom
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Bin:
    n: int
    p_low: float  # smallest forecast in the bin
    p_high: float  # largest forecast in the bin
    mean_p: float  # mean forecast
    observed: float  # observed frequency of the event
    ci: tuple[float, float] | None  # Wilson 95 % CI of the observed frequency


def reliability_bins(p: Sequence[float], o: Sequence[int], n_bins: int = 5) -> list[Bin]:
    """About `n_bins` equal-count bins over the sorted forecasts. Equal forecasts never straddle two bins (the app's
    fixed low/medium/high mapping produces many ties), so there may be fewer bins than asked for."""
    _check(p, o)
    if not p or n_bins < 1:
        return []
    pairs = sorted(zip(p, o, strict=True), key=lambda t: t[0])
    n, target = len(pairs), len(pairs) / n_bins
    groups: list[list[tuple[float, int]]] = []
    cur: list[tuple[float, int]] = []
    for i, pair in enumerate(pairs):
        cur.append(pair)
        nxt = pairs[i + 1][0] if i + 1 < n else None
        filled = sum(len(g) for g in groups) + len(cur)
        if nxt is None or (filled >= target * (len(groups) + 1) and nxt != pair[0]):
            groups.append(cur)
            cur = []
    out = []
    for g in groups:
        k = sum(y for _, y in g)
        out.append(Bin(n=len(g), p_low=g[0][0], p_high=g[-1][0], mean_p=sum(x for x, _ in g) / len(g),
                       observed=k / len(g), ci=wilson(k, len(g))))  # fmt: skip
    return out


def hit_rate(
    p: Sequence[float], o: Sequence[int]
) -> tuple[int, int, float | None, tuple[float, float] | None]:
    """(hits, directional calls, rate, Wilson CI). A call is directional when p ≠ 0.5; it is a hit when p > 0.5 and
    the event happened, or p < 0.5 and it did not."""
    _check(p, o)
    calls = [(pi, oi) for pi, oi in zip(p, o, strict=True) if pi != 0.5]
    hits = sum((pi > 0.5) == (oi == 1) for pi, oi in calls)
    n = len(calls)
    return hits, n, (hits / n if n else None), wilson(hits, n)


@dataclass
class Summary:
    n: int
    base_rate: float | None
    mean_p: float | None
    brier: float | None
    brier_reference: float | None
    brier_skill: float | None
    log_loss: float | None
    hits: int
    calls: int
    hit_rate: float | None
    hit_rate_ci: tuple[float, float] | None
    bins: list[Bin]

    def to_json(self) -> dict:
        out = asdict(self)
        out["hit_rate_ci"] = list(self.hit_rate_ci) if self.hit_rate_ci else None
        for b in out["bins"]:
            b["ci"] = list(b["ci"]) if b["ci"] else None
        return out


def summarize(p: Sequence[float], o: Sequence[int], n_bins: int = 5) -> Summary:
    hits, calls, rate, ci = hit_rate(p, o)
    return Summary(n=len(p), base_rate=base_rate(o), mean_p=(sum(p) / len(p)) if p else None, brier=brier(p, o),
                   brier_reference=brier_reference(o), brier_skill=brier_skill(p, o), log_loss=log_loss(p, o),
                   hits=hits, calls=calls, hit_rate=rate, hit_rate_ci=ci, bins=reliability_bins(p, o, n_bins))  # fmt: skip

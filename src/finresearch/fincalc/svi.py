"""Arbitrage-free SVI smiles per expiry (#386, P3.3): raw SVI, its SVI-JW form, the static no-arbitrage checks and a
numpy-only quasi-explicit calibration. A fit that fails, or whose smile has butterfly, wing or negative-variance
arbitrage, is unknown with the reason: never a clipped or silently repaired smile.

    k = ln(K/F),  w(k) = sigma_imp(k)^2 T  (total implied variance; T on any clock: a slice is clock-free)
    raw SVI:  w(k) = a + b [rho (k - m) + sqrt((k - m)^2 + s^2)],   b >= 0, |rho| < 1, s > 0
    butterfly-free iff g(k) >= 0 for all k and d+(k) -> -inf as k -> inf (Gatheral and Jacquier 2014, Lemma 2.2):
        g(k) = (1 - k w'/(2w))^2 - (w'^2/4)(1/w + 1/4) + w''/2
    Lee's moment formula bounds the wings: w(k)/|k| -> b (1 +/- rho) <= 2 (Lee 2004, Math. Finance 14(3), 469-480)
    calendar-free iff w(k, T1) <= w(k, T2) for T1 < T2 (GJ 2014, Lemma 2.1)

Source: J. Gatheral and A. Jacquier, "Arbitrage-free SVI volatility surfaces", Quantitative Finance 14(1), 59-71
(2014), arXiv:1204.0646: eq. (3.1) raw SVI, (3.5) SVI-JW, Lemma 3.2 the inverse, Example 3.1 Vogt's arbitrageable
smile and Example 5.1 its repair (the test cases). The calibration is Zeliade Systems' quasi-explicit method
(Zeliade white paper ZWP-0005, "Quasi-explicit calibration of Gatheral's SVI model", 2009).

g(k) has no closed-form minimum, so the butterfly check evaluates it on a grid that spans both the far wings and the
smile's own scale around m, then refines the worst cell. Pure functions, no I/O; the live card does not use this
(switching is the horse race's decision, #377).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# The static-arbitrage check's grid (registered with horserace addendum A4):
K_WIDE = 3.0  # |k| up to 3 (strikes from 5 % to 2,000 % of the forward), evenly spaced ...
K_LOCAL = 25.0  # ... plus m +/- 25 s, where a narrow (weekly) smile bends
GRID_POINTS = 4001  # points in each of the two ranges
LEE_MAX = 2.0  # b (1 + |rho|) <= 2 (Lee's moment formula for total variance)
CAL_TOL = 1e-12  # a near slice may exceed the far one by no more than this (rounding) in total variance


@dataclass(frozen=True)
class Raw:
    a: float
    b: float
    m: float
    rho: float
    s: float


@dataclass(frozen=True)
class Jw:
    """SVI-JW at expiry t: ATM variance v, ATM skew psi, put and call wing slopes p and c, minimum variance v_min."""

    v: float
    psi: float
    p: float
    c: float
    v_min: float


def w(p: Raw, k: float | np.ndarray) -> float | np.ndarray:
    x = k - p.m
    return p.a + p.b * (p.rho * x + np.sqrt(x * x + p.s * p.s))


def dw(p: Raw, k: float | np.ndarray) -> float | np.ndarray:
    x = k - p.m
    return p.b * (p.rho + x / np.sqrt(x * x + p.s * p.s))


def d2w(p: Raw, k: float | np.ndarray) -> float | np.ndarray:
    x = k - p.m
    return p.b * p.s * p.s / (x * x + p.s * p.s) ** 1.5


def g(p: Raw, k: float | np.ndarray) -> float | np.ndarray:
    """Gatheral-Jacquier's g (2.1): the density is g(k) / sqrt(2 pi w) exp(-d-^2/2), so g < 0 is a negative density."""
    w0, w1, w2 = w(p, k), dw(p, k), d2w(p, k)
    return (1 - k * w1 / (2 * w0)) ** 2 - w1 * w1 / 4 * (1 / w0 + 0.25) + w2 / 2


def to_jw(p: Raw, t: float) -> Jw:
    """GJ 2014 eq. (3.5)."""
    root = math.sqrt(p.m * p.m + p.s * p.s)
    wt = p.a + p.b * (-p.rho * p.m + root)
    sq = math.sqrt(wt)
    return Jw(wt / t, p.b / 2 * (p.rho - p.m / root) / sq, p.b * (1 - p.rho) / sq, p.b * (1 + p.rho) / sq,
              (p.a + p.b * p.s * math.sqrt(1 - p.rho * p.rho)) / t)  # fmt: skip


def from_jw(j: Jw, t: float) -> Raw | None:
    """GJ 2014 Lemma 3.2 (the inverse of eq. 3.5); None when the JW set is not a convex raw-SVI smile
    (beta outside [-1, 1], i.e. not -p <= 2 psi <= c) or a parameter is degenerate."""
    wt = j.v * t
    if wt <= 0 or j.c + j.p <= 0:
        return None
    sq = math.sqrt(wt)
    b = sq / 2 * (j.c + j.p)
    rho = 1 - j.p * sq / b
    if not -1 < rho < 1:
        return None
    beta = rho - 2 * j.psi * sq / b
    if beta == 0:  # m = 0: the lemma's s = (v t - a)/b, with a from v_min, solved for s
        root = 1 - math.sqrt(1 - rho * rho)
        s = (wt - j.v_min * t) / (b * root) if root > 0 else -1.0
        return None if s <= 0 else Raw(j.v_min * t - b * s * math.sqrt(1 - rho * rho), b, 0.0, rho, s)
    if not -1 < beta < 1:
        return None
    alpha = math.copysign(math.sqrt(1 / (beta * beta) - 1), beta)
    denom = b * (
        -rho + math.copysign(1.0, alpha) * math.sqrt(1 + alpha * alpha) - alpha * math.sqrt(1 - rho * rho)
    )
    if denom == 0:
        return None
    m = (j.v - j.v_min) * t / denom
    s = alpha * m
    if s <= 0:
        return None
    a = j.v_min * t - b * s * math.sqrt(1 - rho * rho)
    return Raw(a, b, m, rho, s)


# --------------------------------------------------------------------------- static arbitrage
def grid(*ps: Raw) -> np.ndarray:
    """The check grid: [-K_WIDE, K_WIDE] evenly, plus m +/- K_LOCAL s for each slice."""
    parts = [np.linspace(-K_WIDE, K_WIDE, GRID_POINTS)]
    parts += [np.linspace(p.m - K_LOCAL * p.s, p.m + K_LOCAL * p.s, GRID_POINTS) for p in ps]
    return np.unique(np.concatenate(parts))


def _refine(f, lo: float, hi: float, iters: int = 80) -> tuple[float, float]:
    """Golden-section minimum of f on [lo, hi]: (k, f(k))."""
    r = (math.sqrt(5) - 1) / 2
    x1, x2 = hi - r * (hi - lo), lo + r * (hi - lo)
    f1, f2 = f(x1), f(x2)
    for _ in range(iters):
        if f1 <= f2:
            hi, x2, f2 = x2, x1, f1
            x1 = hi - r * (hi - lo)
            f1 = f(x1)
        else:
            lo, x1, f1 = x1, x2, f2
            x2 = lo + r * (hi - lo)
            f2 = f(x2)
    return (x1, f1) if f1 <= f2 else (x2, f2)


@dataclass(frozen=True)
class Check:
    ok: bool
    reason: str | None
    w_min: float  # min over k of w (a + b s sqrt(1 - rho^2), exact)
    lee: float  # b (1 + |rho|)
    lee_ok: bool
    g_min: float  # min of g on the grid, refined
    g_min_k: float


def check(p: Raw) -> Check:
    """Every static condition on one slice; `reason` names the first that fails (None when the smile is free)."""
    w_min = p.a + p.b * p.s * math.sqrt(max(0.0, 1 - p.rho * p.rho))
    lee = p.b * (1 + abs(p.rho))
    ks = grid(p)
    with np.errstate(divide="ignore", invalid="ignore"):
        gs = np.asarray(g(p, ks), dtype=float)
    gs = np.where(np.isfinite(gs), gs, -np.inf)
    i = int(np.argmin(gs))
    k0, g0 = float(ks[i]), float(gs[i])
    if math.isfinite(g0) and 0 < i < len(ks) - 1:
        k0, g0 = _refine(lambda k: float(g(p, k)), float(ks[i - 1]), float(ks[i + 1]))
    lee_ok = lee <= LEE_MAX
    why = None
    if not (p.b >= 0 and -1 < p.rho < 1 and p.s > 0):
        why = "parameters outside b >= 0, |rho| < 1, s > 0"
    elif w_min <= 0:
        why = f"negative total variance: min w = {w_min:.3g}"
    elif not lee_ok:
        why = f"Lee's wing bound fails: b (1 + |rho|) = {lee:.4g} > {LEE_MAX:g}"
    elif g0 < 0:
        why = f"butterfly arbitrage: g = {g0:.3g} < 0 at k = {k0:.4f} (negative density)"
    return Check(why is None, why, w_min, lee, lee_ok, g0, k0)


@dataclass(frozen=True)
class Calendar:
    ok: bool
    worst: float  # max over the grid of w_near - w_far (> CAL_TOL: calendar arbitrage)
    worst_k: float | None


def calendar_ok(near: Raw, far: Raw) -> Calendar:
    """w(k, T_near) <= w(k, T_far) on the check grid of both slices (GJ 2014 Lemma 2.1, sampled)."""
    ks = grid(near, far)
    d = np.asarray(w(near, ks)) - np.asarray(w(far, ks))
    i = int(np.argmax(d))
    worst = float(d[i])
    return Calendar(worst <= CAL_TOL, worst, float(ks[i]) if worst > CAL_TOL else None)

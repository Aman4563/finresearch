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


# --------------------------------------------------------------------------- calibration
# Zeliade's quasi-explicit method: with y = (k - m)/s and z = sqrt(y^2 + 1), raw SVI is linear in (a, d, c) =
# (a, rho b s, b s): w = a + d y + c z. For fixed (m, s) the weighted least squares in (a, d, c) is solved exactly
# over the admissible set (c, d) in {|d| <= RHO_MAX c, |d| <= 2 s - c} (b >= 0, |rho| <= RHO_MAX and Lee's
# b (1 + |rho|) <= 2; Zeliade's paper writes 4 s, a bound on sigma^2 rather than on total variance), with a profiled
# out; an optimum outside the set is the best point on its boundary (a convex quadratic: the minimum over each edge),
# never the clipped unconstrained point. Then (m, s) by Nelder-Mead from the best of a coarse grid.
RHO_MAX = 0.999  # |rho| < 1 strictly
MIN_POINTS = 6  # five parameters plus one degree of freedom
S_MIN, S_MAX = 1e-4, 2.0  # the box of s
M_PAD = 1.0  # m within [k_min - pad x span, k_max + pad x span]
START_GRID = 9  # a 9 x 9 grid over (m, ln s) chooses the Nelder-Mead start
NM_STEP, NM_ITER, NM_FTOL = 0.1, 400, 1e-14
MAX_RMSE = 0.01  # a fit whose IV RMSE exceeds 1 vol point is refused (unknown)
TARGET_RMSE = 0.005  # issue #386 acceptance (a): <= 0.5 vol points on liquid slices; above it is flagged


def nelder_mead(f, x0: list[float], *, step: float = NM_STEP, max_iter: int = NM_ITER,
                ftol: float = NM_FTOL) -> tuple[list[float], float]:  # fmt: skip
    """The standard Nelder-Mead simplex (reflection 1, expansion 2, contraction 1/2, shrink 1/2), stopping when the
    simplex's values differ by under ftol or after max_iter steps. Returns the best point and its value."""
    n = len(x0)
    pts = [list(x0)] + [[x0[j] + (step if j == i else 0.0) for j in range(n)] for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(max_iter):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) <= ftol and all(math.isfinite(v) for v in vals):
            break
        cen = [sum(p[j] for p in pts[:-1]) / n for j in range(n)]
        refl = [cen[j] + (cen[j] - pts[-1][j]) for j in range(n)]
        fr = f(refl)
        if fr < vals[0]:
            exp = [cen[j] + 2 * (cen[j] - pts[-1][j]) for j in range(n)]
            fe = f(exp)
            pts[-1], vals[-1] = (exp, fe) if fe < fr else (refl, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = refl, fr
        else:
            con = [cen[j] + 0.5 * (pts[-1][j] - cen[j]) for j in range(n)]
            fc = f(con)
            if fc < vals[-1]:
                pts[-1], vals[-1] = con, fc
            else:
                pts = [pts[0]] + [[pts[0][j] + 0.5 * (p[j] - pts[0][j]) for j in range(n)] for p in pts[1:]]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    i = min(range(n + 1), key=lambda i: vals[i])
    return pts[i], vals[i]


@dataclass(frozen=True)
class Inner:
    a: float
    d: float
    c: float
    sse: float
    boundary: bool  # the unconstrained optimum was outside the admissible set


def inner(ks, ws, wts, m: float, s: float) -> Inner:
    """The exact constrained weighted least squares in (a, d, c) at fixed (m, s)."""
    k, wv, wt = np.asarray(ks, float), np.asarray(ws, float), np.asarray(wts, float)
    y = (k - m) / s
    z = np.sqrt(y * y + 1)
    sw = wt.sum()
    # profile a out: centre every column on its weighted mean
    yc, zc, tc = y - (wt @ y) / sw, z - (wt @ z) / sw, wv - (wt @ wv) / sw
    h = np.array([[wt @ (yc * yc), wt @ (yc * zc)], [wt @ (yc * zc), wt @ (zc * zc)]])
    rhs = np.array([wt @ (yc * tc), wt @ (zc * tc)])

    def sse(d: float, c: float) -> float:
        e = tc - d * yc - c * zc
        return float(wt @ (e * e))

    def admissible(d: float, c: float) -> bool:
        eps = 1e-15
        return c >= -eps and abs(d) <= RHO_MAX * c + eps and abs(d) <= 2 * s - c + eps

    boundary = False
    try:
        d, c = (float(v) for v in np.linalg.solve(h, rhs))
        ok = admissible(d, c)
    except np.linalg.LinAlgError:
        ok = False
    if not ok:
        boundary = True
        c1 = 2 * s / (1 + RHO_MAX)
        verts = [
            (0.0, 0.0),
            (RHO_MAX * c1, c1),
            (0.0, 2 * s),
            (-RHO_MAX * c1, c1),
        ]  # (d, c), convex, in order
        best = None
        for i in range(4):
            p, q = np.array(verts[i]), np.array(verts[(i + 1) % 4])
            e = q - p
            den = float(e @ h @ e)
            u = 0.0 if den <= 0 else min(1.0, max(0.0, float(e @ (rhs - h @ p)) / den))
            cand = p + u * e
            val = sse(float(cand[0]), float(cand[1]))
            if best is None or val < best[0]:
                best = (val, float(cand[0]), float(cand[1]))
        _, d, c = best  # type: ignore[misc]
    a = float(wt @ (wv - d * y - c * z)) / sw
    return Inner(a, d, c, sse(d, c), boundary)


def to_raw(i: Inner, m: float, s: float) -> Raw:
    b = i.c / s
    return Raw(i.a, b, m, i.d / i.c if i.c > 0 else 0.0, s)


@dataclass(frozen=True)
class Point:
    k: float  # ln(K/F)
    sigma: float  # implied volatility (annual fraction) on the caller's clock
    band: float | None = None  # its error band (fraction); None = unknown


@dataclass(frozen=True)
class Fit:
    params: Raw | None  # None = unknown (reason); never a repaired smile
    t: float
    n: int = 0
    rmse_vol: float | None = None  # root mean square IV error, annual fraction (0.005 = 0.5 vol points)
    max_err_vol: float | None = None
    weights: str = "equal"  # equal (in volatility) | band (1 / band^2)
    flags: tuple[str, ...] = ()
    reason: str | None = None
    raw: Raw | None = None  # the calibrated parameters even when refused, for diagnosis only
    check: Check | None = None


def fit(points: list[Point], t: float) -> Fit:
    """The raw-SVI smile of one expiry through `points`; unknown with the reason when there are too few points, the
    fit is poor (RMSE above MAX_RMSE) or the fitted smile is not free of static arbitrage."""
    pts = [p for p in points if all(math.isfinite(x) for x in (p.k, p.sigma)) and p.sigma > 0]
    if not (math.isfinite(t) and t > 0):
        return Fit(None, t, len(pts), reason="time to expiry must be positive")
    if len(pts) < MIN_POINTS or len({p.k for p in pts}) < MIN_POINTS:
        return Fit(None, t, len(pts), reason=f"fewer than {MIN_POINTS} strikes with an implied volatility")
    ks = [p.k for p in pts]
    ws = [p.sigma * p.sigma * t for p in pts]
    banded = all(p.band is not None and p.band > 0 for p in pts)
    # residuals are in total variance; dw = 2 sigma T dsigma, so these weights make the sum one of IV errors^2 / band^2
    wts = [1 / (2 * p.sigma * t * (p.band if banded else 1.0)) ** 2 for p in pts]  # type: ignore[operator]
    lo, hi = min(ks), max(ks)
    span = hi - lo
    m_lo, m_hi = lo - M_PAD * span, hi + M_PAD * span
    l_lo, l_hi = math.log(S_MIN), math.log(S_MAX)

    def obj(x: list[float]) -> float:
        m, ls = x
        if not (m_lo <= m <= m_hi and l_lo <= ls <= l_hi):
            return math.inf  # outside the box: rejected, not projected
        return inner(ks, ws, wts, m, math.exp(ls)).sse

    starts = [[m_lo + (m_hi - m_lo) * i / (START_GRID - 1), l_lo + (l_hi - l_lo) * j / (START_GRID - 1)]
              for i in range(START_GRID) for j in range(START_GRID)]  # fmt: skip
    x0 = min(starts, key=obj)
    x, _ = nelder_mead(obj, x0)
    m, s = x[0], math.exp(x[1])
    sol = inner(ks, ws, wts, m, s)
    p = to_raw(sol, m, s)
    flags = []
    if sol.boundary:
        flags.append("on the constraint boundary")
    if min(abs(x[1] - l_lo), abs(x[1] - l_hi)) < 1e-6 or min(abs(m - m_lo), abs(m - m_hi)) < 1e-9:
        flags.append("on the (m, s) box")
    with np.errstate(invalid="ignore"):
        fitted = np.sqrt(np.maximum(np.asarray(w(p, np.asarray(ks))), 0.0) / t)
    errs = fitted - np.array([q.sigma for q in pts])
    rmse, mx = float(np.sqrt(np.mean(errs * errs))), float(np.max(np.abs(errs)))
    if rmse > TARGET_RMSE:
        flags.append(f"RMSE {rmse * 100:.2f} vol points is above the {TARGET_RMSE * 100:g}-point target")
    chk = check(p)
    out = Fit(p, t, len(pts), rmse, mx, "band" if banded else "equal", tuple(flags), None, p, chk)
    if not chk.ok:
        return Fit(None, t, len(pts), rmse, mx, out.weights, out.flags,
                   f"the fitted smile is not arbitrage-free: {chk.reason}", p, chk)  # fmt: skip
    if rmse > MAX_RMSE:
        return Fit(None, t, len(pts), rmse, mx, out.weights, out.flags,
                   f"RMSE {rmse * 100:.2f} vol points is above {MAX_RMSE * 100:g}", p, chk)  # fmt: skip
    return out

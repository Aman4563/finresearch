"""#520: the exact touch probability against a brute-force Monte Carlo that uses no reflection formula, and the
continuity-correction option against a daily-step Monte Carlo. Synthetic paths only; the seed (the issue number) was
fixed before the first run and is not tuned.

A path sampled on a grid misses the touches between its points, so a plain fine-grid estimate is biased low by a term
proportional to sqrt(dt) (that bias is what Broadie, Glasserman and Kou's correction prices). The estimate here uses
the same paths on two nested grids, n and n / 4 steps, and removes the leading term: with p(n) = p - c / sqrt(n),
p = 2 p(n) - p(n / 4). Its Monte Carlo error is the standard error of the per-path values 2 hit_n - hit_{n/4}.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from finresearch.fincalc import risk_alerts as ra

SEED = 520
F = 24000.0


def _paths(vol: float, t: float, steps: int, paths: int, every: int) -> tuple[np.ndarray, ...]:
    """Running minimum and maximum of ln(F_t / F_0) on the fine grid and on every `every`-th point of it, under
    d ln F = -sigma^2 / 2 dt + sigma dW (exact Gaussian steps)."""
    rng = np.random.default_rng(SEED)
    dt = t / steps
    x = np.zeros(paths)
    lo_f, hi_f, lo_c, hi_c = (np.zeros(paths) for _ in range(4))
    for i in range(1, steps + 1):
        x += -vol * vol / 2 * dt + vol * math.sqrt(dt) * rng.standard_normal(paths)
        np.minimum(lo_f, x, out=lo_f)
        np.maximum(hi_f, x, out=hi_f)
        if i % every == 0:
            np.minimum(lo_c, x, out=lo_c)
            np.maximum(hi_c, x, out=hi_c)
    return lo_f, hi_f, lo_c, hi_c


def test_a_fine_grid_monte_carlo_agrees_with_the_exact_form_and_rejects_the_shortcut() -> None:
    """60 % volatility, 90 days (sigma sqrt(T) = 0.30, where the two variants are furthest apart), strikes 10 % below
    and above the forward; 200,000 paths on 1,024 and 256 steps. Stated Monte Carlo error: the standard error of the
    extrapolated estimate, about 0.001; the exact form must be within 4 of them and the shortcut more than 20 away."""
    vol, t, paths = 0.60, 90 / 365, 200_000
    lo_f, hi_f, lo_c, hi_c = _paths(vol, t, 1024, paths, every=4)
    ests: dict[float, float] = {}
    for m, fine, coarse in ((0.90, lo_f <= math.log(0.90), lo_c <= math.log(0.90)),
                            (1.10, hi_f >= math.log(1.10), hi_c >= math.log(1.10))):  # fmt: skip
        y = 2.0 * fine - coarse
        est, se = float(y.mean()), float(y.std(ddof=1)) / math.sqrt(paths)
        exact = ra.touch_prob_exact(F, F * m, vol, t)
        shortcut = ra.touch_prob(F, F * m, vol, t)
        assert 0.0008 < se < 0.0015
        assert abs(est - exact) < 4 * se, (m, est, se, exact)
        assert abs(est - shortcut) > 20 * se, (m, est, se, shortcut)
        # the plain fine-grid estimate is low by the missed touches, and the coarser grid by about twice as much
        assert float(coarse.mean()) < float(fine.mean()) < exact
        assert (exact - coarse.mean()) / (exact - fine.mean()) == pytest.approx(2.0, abs=0.35)
        ests[m] = est
    # the sign of the shortcut's error, seen by the simulation alone: too high below the forward; above it, lower
    # than even the plain fine-grid estimate, which is itself biased low
    assert ra.touch_prob(F, F * 0.90, vol, t) > ests[0.90] + 0.05
    assert ra.touch_prob(F, F * 1.10, vol, t) < float((hi_f >= math.log(1.10)).mean()) - 0.03


def test_the_continuity_correction_tracks_a_daily_step_monte_carlo() -> None:
    """15 % volatility, 30 days watched at 21 equally spaced closes, strikes 5 % away; 400,000 paths (standard error
    about 0.0006). The continuous formula overstates the daily-close touch probability by about 0.045; the shifted
    barrier is a first-order correction, so what is left is its truncation error (about 0.002 at 21 dates) plus the
    Monte Carlo error: within 0.005, and at least 90 % of the gap removed."""
    vol, t, m, paths = 0.15, 30 / 365, 21, 400_000
    rng = np.random.default_rng(SEED)
    z = rng.standard_normal((paths, m))
    x = np.cumsum(-vol * vol / 2 * t / m + vol * math.sqrt(t / m) * z, axis=1)
    for mny, hit in ((0.95, x.min(axis=1) <= math.log(0.95)), (1.05, x.max(axis=1) >= math.log(1.05))):
        p = float(hit.mean())
        se = math.sqrt(p * (1 - p) / paths)
        continuous = ra.touch_prob_exact(F, F * mny, vol, t)
        corrected = ra.touch_prob_exact(F, F * mny, vol, t, monitoring_dates=m)
        assert se < 0.0007
        assert continuous - p > 0.04  # daily closes see fewer touches: far outside the Monte Carlo error
        assert abs(corrected - p) < 0.005, (mny, p, se, corrected)
        assert abs(corrected - p) < 0.1 * (continuous - p)

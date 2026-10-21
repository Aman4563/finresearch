"""Statistics of the live skill monitor (#378, registered in skillmon/specs.py). Pure functions; each returns None
(unknown) where its statistic is undefined, never 0. Murphy, Kupiec and band coverage are horserace.scores'.

- Paired loss differential d_i = (p_m,i - o_i)² - (p_b,i - o_i)²; BSS = 1 - Σ(p_m - o)² / Σ(p_b - o)².
- Moving-block bootstrap (Künsch 1989): blocks of length L = max(h, ⌈n^(1/3)⌉) start uniformly in [0, n - L],
  ⌈n / L⌉ of them are joined and cut to n. The CI is BSS ± z·sd of the resampled BSS (the normal interval with the
  block-bootstrap standard error): on issue #378's acceptance panels it covered 93.6 % against 92.9 % for the
  percentile interval (evals/experiments/skill_monitoring/PREREG.md), which is reported beside it.
- CITL = mean(o) - mean(p). Spiegelhalter (1986) Z = Σ(o - p)(1 - 2p) / √Σ(1 - 2p)² p(1 - p), N(0, 1), two-sided.
- Risk-adjusted Bernoulli CUSUM (Steiner, Cook, Farewell & Treasure 2000): W_i = o_i ln R - ln(1 - p_i + R p_i),
  S_i = max(0, S_{i-1} + W_i), alarm when S_i > h; two-sided with R = 2 (odds up) and R = 0.5 (odds down).
  S does not depend on h, so a run length for any h is the first index where the running maximum of S exceeds it:
  `calibrate_h` bisects h on one set of simulated paths (common random numbers).
- PSI = Σ_b (a_b - e_b) ln(a_b / e_b) on bin shares. Pinball L_τ(q, y) = (y - q)(τ - 1{y < q}). Adaptive conformal
  (Gibbs & Candès 2021) α_{t+1} = α_t + γ(α - err_t). P&L CUSUM S_t = max(0, S_{t-1} + (μ0 - x_t) - k).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist
from typing import Any

import numpy as np


def loss_diff(pm: Sequence[float], pb: Sequence[float], o: Sequence[int]) -> list[float]:
    return [(a - y) ** 2 - (b - y) ** 2 for a, b, y in zip(pm, pb, o, strict=True)]


def bss(pm: Sequence[float], pb: Sequence[float], o: Sequence[int]) -> float | None:
    lb = sum((b - y) ** 2 for b, y in zip(pb, o, strict=True))
    if len(o) == 0 or lb == 0:
        return None
    return 1 - sum((a - y) ** 2 for a, y in zip(pm, o, strict=True)) / lb


def block_len(n: int, h: int) -> int:
    return max(h, math.ceil(round(n ** (1 / 3), 9)))


def bss_ci(pm: Sequence[float], pb: Sequence[float], o: Sequence[int], *, h: int, reps: int, seed: int,
           level: float = 0.95) -> dict[str, Any]:  # fmt: skip
    """BSS with a moving-block bootstrap CI (module docstring). Rows in time order."""
    n = len(o)
    out: dict[str, Any] = {
        "n": n,
        "bss": bss(pm, pb, o),
        "lo": None,
        "hi": None,
        "se": None,
        "pct": None,
        "block": None,
        "reps": reps,
    }
    if out["bss"] is None or n < 2:
        return out
    L = min(block_len(n, h), n)
    lm = (np.asarray(pm, float) - np.asarray(o, float)) ** 2
    lb = (np.asarray(pb, float) - np.asarray(o, float)) ** 2
    rng = np.random.default_rng(seed)
    k = math.ceil(n / L)
    starts = rng.integers(0, n - L + 1, size=(reps, k))
    idx = (starts[:, :, None] + np.arange(L)).reshape(reps, -1)[:, :n]
    den = lb[idx].sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        b = np.where(den > 0, 1 - lm[idx].sum(axis=1) / den, np.nan)
    b = b[np.isfinite(b)]
    if len(b) < reps / 2:  # most resamples undefined: the interval is unknown
        out["block"] = L
        return out
    a, se = (1 - level) / 2, float(np.std(b))
    z = NormalDist().inv_cdf(1 - a)
    out.update(
        lo=out["bss"] - z * se,
        hi=out["bss"] + z * se,
        se=se,
        block=L,
        pct=[float(np.quantile(b, a)), float(np.quantile(b, 1 - a))],
    )
    return out


def citl(p: Sequence[float], o: Sequence[int]) -> float | None:
    return (sum(o) - sum(p)) / len(o) if len(o) else None


def spiegelhalter(p: Sequence[float], o: Sequence[int]) -> dict[str, float | None]:
    var = sum((1 - 2 * a) ** 2 * a * (1 - a) for a in p)
    if len(o) == 0 or var <= 0:
        return {"z": None, "p": None}
    z = sum((y - a) * (1 - 2 * a) for a, y in zip(p, o, strict=True)) / math.sqrt(var)
    return {"z": z, "p": math.erfc(abs(z) / math.sqrt(2))}


# --------------------------------------------------------------------------- Bernoulli CUSUM
def cusum_weights(p: np.ndarray, o: np.ndarray, R: float) -> np.ndarray:
    return o * math.log(R) - np.log(1 - p + R * p)


def cusum_path(p: Sequence[float], o: Sequence[int], R: float) -> list[float]:
    s, out = 0.0, []
    for w in cusum_weights(np.asarray(p, float), np.asarray(o, float), R):
        s = max(0.0, s + float(w))
        out.append(s)
    return out


def two_sided_cusum(p: Sequence[float], o: Sequence[int], *, h_up: float, h_down: float,
                    r_up: float = 2.0, r_down: float = 0.5) -> dict[str, Any]:  # fmt: skip
    """Both charts over the events in time order; `alarm` is the first crossing (0-based index), else None."""
    up, down = cusum_path(p, o, r_up), cusum_path(p, o, r_down)
    first = [
        (i, side)
        for side, path, h in (("up", up, h_up), ("down", down, h_down))
        for i, v in enumerate(path)
        if v > h
    ]
    hit = min(first, default=None)
    return {"n": len(o), "s_up": up[-1] if up else 0.0, "s_down": down[-1] if down else 0.0,
            "alarm": None if hit is None else {"side": hit[1], "at": hit[0]}}  # fmt: skip


def shifted(p: np.ndarray, odds: float) -> np.ndarray:
    """The probability whose odds are `odds` times p's."""
    return odds * p / (1 - p + odds * p)


def simulate_cummax(p_pool: Sequence[float], R: float, *, runs: int, length: int, seed: int,
                    shift_after: int | None = None, odds: float = 1.0) -> np.ndarray:  # fmt: skip
    """Running maxima of S on `runs` simulated paths: p drawn i.i.d. from `p_pool`, o ~ Bernoulli(p) (in control),
    or with the odds multiplied by `odds` from event `shift_after` (0-based) on."""
    rng = np.random.default_rng(seed)
    p = rng.choice(np.asarray(p_pool, float), size=(runs, length))
    true = p.copy()
    if shift_after is not None:
        true[:, shift_after:] = shifted(p[:, shift_after:], odds)
    o = (rng.random((runs, length)) < true).astype(float)
    w = cusum_weights(p, o, R)
    s = np.zeros(runs)
    cm = np.empty((runs, length))
    best = np.zeros(runs)
    for t in range(length):
        s = np.maximum(0.0, s + w[:, t])
        best = np.maximum(best, s)
        cm[:, t] = best
    return cm


def run_lengths(cm: np.ndarray, h: float) -> np.ndarray:
    """Events until the first S > h (1-based); a path that never crosses counts its full length (censored)."""
    hit = cm > h
    return np.where(hit.any(axis=1), hit.argmax(axis=1) + 1, cm.shape[1])


def calibrate_h(cm: np.ndarray, target: float, lo: float = 0.0, hi: float = 50.0, iters: int = 60) -> float:
    """The h whose mean simulated run length is `target` (bisection; ARL rises with h)."""
    for _ in range(iters):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if run_lengths(cm, mid).mean() < target else (lo, mid)
    return round((lo + hi) / 2, 4)


def calibrate_two_sided(cm_up: np.ndarray, cm_down: np.ndarray, target: float) -> tuple[float, float]:
    """(h_up, h_down) with equal one-sided ARLs whose two-sided run length min(RL_up, RL_down) averages `target`."""
    lo, hi = target, 4 * target
    for _ in range(40):
        side = (lo + hi) / 2
        hu, hd = calibrate_h(cm_up, side), calibrate_h(cm_down, side)
        arl = np.minimum(run_lengths(cm_up, hu), run_lengths(cm_down, hd)).mean()
        lo, hi = (side, hi) if arl < target else (lo, side)
    side = (lo + hi) / 2
    return calibrate_h(cm_up, side), calibrate_h(cm_down, side)


# --------------------------------------------------------------------------- drift, intervals, P&L
def psi(expected: Sequence[float], actual: Sequence[float], eps: float = 1e-4) -> float | None:
    """PSI on bin counts (or shares); empty bins are floored at `eps` share. None when either side is empty."""
    te, ta = sum(expected), sum(actual)
    if te <= 0 or ta <= 0 or len(expected) != len(actual):
        return None
    e = [max(x / te, eps) for x in expected]
    a = [max(x / ta, eps) for x in actual]
    return sum((y - x) * math.log(y / x) for x, y in zip(e, a, strict=True))


def pinball(q: float, y: float, tau: float) -> float:
    return (y - q) * (tau - (1.0 if y < q else 0.0))


def aci_step(alpha_t: float, alpha: float, err: int, gamma: float) -> float:
    return alpha_t + gamma * (alpha - err)


def pnl_cusum(x: Sequence[float], *, mu0: float, k: float) -> list[float]:
    s, out = 0.0, []
    for v in x:
        s = max(0.0, s + (mu0 - v) - k)
        out.append(s)
    return out

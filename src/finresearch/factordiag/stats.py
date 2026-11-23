"""Statistics of the stock factor diagnostics (#506, evals/experiments/stock_factor_diagnostics/PREREG.md): pure
functions on arrays, no file, database or network access. Every default is the registered constant of specs.py.
Unknown is None, never 0: a statistic whose inputs are too few or degenerate is not computed.

- Rank IC: Spearman (1904), the Pearson correlation of average ranks (Grinold & Kahn 2000, ch. 6, for its use).
- Newey & West (1987) Bartlett standard error of a mean, via direction.metrics.newey_west_var (reported only).
- Interval: Student t on the means of consecutive blocks (Ibragimov & Mueller 2010, JBES 28(4)).
- Family A: Fisher's (1935) sign-flip randomisation test applied to block sums; family B: the two-sample permutation
  test (Efron & Tibshirani 1993, ch. 15) applied to block means. Benjamini & Hochberg (1995) q-values.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from itertools import combinations
from typing import Any

import numpy as np

from finresearch.direction.metrics import newey_west_var
from finresearch.factordiag import specs
from finresearch.horserace.scores import t_quantile


# --------------------------------------------------------------------------------------------- ranks and the IC
def average_ranks(x: Sequence[float]) -> np.ndarray:
    """Ascending ranks 1..n; tied values share the average of the ranks they span."""
    a = np.asarray(x, dtype=float)
    order = np.argsort(a, kind="mergesort")
    s = a[order]
    first = np.flatnonzero(np.r_[True, s[1:] != s[:-1]]) if len(a) else np.array([], dtype=int)
    counts = np.diff(np.r_[first, len(a)])
    out = np.empty(len(a))
    out[order] = np.repeat(first + (counts + 1) / 2, counts)
    return out


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman's rank correlation; None when fewer than two pairs or either side is constant."""
    if len(x) != len(y):
        raise ValueError("x and y must have the same length")
    if len(x) < 2:
        return None
    a, b = average_ranks(x), average_ranks(y)
    a, b = a - a.mean(), b - b.mean()
    den = math.sqrt(float(a @ a) * float(b @ b))
    return float(a @ b) / den if den > 0 else None


def quintiles(score: Sequence[float], q: int = specs.QUANTILES) -> np.ndarray:
    """Quantile 1..q of each value by its average rank r: min(q, floor(q (r - 0.5) / n) + 1); ties share one."""
    r = average_ranks(score)
    return np.minimum(q, np.floor(q * (r - 0.5) / max(1, len(r))).astype(int) + 1)


# --------------------------------------------------------------------------------------------- one series
def summary(x: Sequence[float]) -> dict[str, Any]:
    """n, mean, sample sd, ICIR = mean / sd (per period) and the share of positive values; None where undefined."""
    a = np.asarray(x, dtype=float)
    n = len(a)
    mean = float(a.mean()) if n else None
    sd = float(a.std(ddof=1)) if n > 1 else None
    return {"n": n, "mean": mean, "sd": sd, "icir": mean / sd if sd else None,
            "share_positive": float(np.mean(a > 0)) if n else None}  # fmt: skip


def newey_west(x: Sequence[float], lag: int) -> dict[str, Any]:
    """Newey-West (Bartlett, `lag` lags) standard error and t of the mean; unknown below lag + 2 values or when the
    variance is not positive."""
    out: dict[str, Any] = {"lag": lag, "se": None, "t": None}
    if len(x) < lag + 2:
        return out
    v = newey_west_var(x, lag)
    if v > 0:
        se = math.sqrt(v)
        out.update(se=se, t=float(np.mean(x)) / se)
    return out


def blocks(n: int, size: int) -> list[slice]:
    """Consecutive blocks of `size` from the first value; a remainder shorter than half a block joins the block
    before it, else it is a block of its own."""
    if size < 1:
        raise ValueError("block size must be at least 1")
    q, rem = divmod(n, size)
    out = [slice(i * size, (i + 1) * size) for i in range(q)]
    if rem:
        if out and rem * 2 < size:
            out[-1] = slice(out[-1].start, n)
        else:
            out.append(slice(q * size, n))
    return out


def block_interval(x: Sequence[float], size: int, *, level: float = specs.CI_LEVEL, power: float = specs.POWER,
                   min_blocks: int = specs.MIN_BLOCKS_CI) -> dict[str, Any]:  # fmt: skip
    """mean +/- t(q - 1) x sd(block means) / sqrt(q) over the q blocks, and the minimum detectable mean at `power`
    from the same standard error; lo, hi, se and mde are None below `min_blocks` blocks."""
    a = np.asarray(x, dtype=float)
    bl = blocks(len(a), size)
    out: dict[str, Any] = {"blocks": len(bl), "block": size, "se": None, "lo": None, "hi": None, "mde": None}
    if len(bl) < max(2, min_blocks):
        return out
    bm = np.array([a[s].mean() for s in bl])
    q = len(bm)
    se = float(bm.std(ddof=1)) / math.sqrt(q)
    tc = t_quantile(1 - (1 - level) / 2, q - 1)
    m = float(a.mean())
    out.update(se=se, lo=m - tc * se, hi=m + tc * se, mde=(tc + t_quantile(power, q - 1)) * se)
    return out


# --------------------------------------------------------------------------------------------- the two nulls
def sign_flip_p(x: Sequence[float], size: int, *, min_blocks: int = specs.MIN_BLOCKS_A,
                exact_max: int = specs.EXACT_MAX_BLOCKS, draws: int = specs.DRAWS, seed: int = specs.SEED,
                tol: float = specs.TOLERANCE) -> dict[str, Any]:  # fmt: skip
    """Family A's null: the two-sided sign-flip randomisation over blocks of dates. S_b = the sum of block b; p = the
    share of sign vectors e with |sum e_b S_b| >= |sum S_b| - tol: all 2^q of them when q <= exact_max (so p is at
    least 2 / 2^q, reported as `min_p`), else `draws` random ones and p = (1 + count) / (1 + draws). p is None below
    `min_blocks` blocks."""
    a = np.asarray(x, dtype=float)
    bl = blocks(len(a), size)
    q = len(bl)
    out: dict[str, Any] = {"p": None, "blocks": q, "block": size, "min_p": None, "exact": None}
    if q < max(1, min_blocks):
        return out
    s = np.array([a[b].sum() for b in bl])
    obs = abs(float(s.sum()))
    if q <= exact_max:
        tot = np.zeros(1)
        for v in s:  # every signed sum, doubling per block: 2^q floats, never a 2^q x q matrix
            tot = np.concatenate([tot + v, tot - v])
        out.update(p=float(np.mean(np.abs(tot) >= obs - tol)), min_p=2.0 / 2**q, exact=True)
    else:
        e = np.random.default_rng(seed).choice([-1.0, 1.0], size=(draws, q))
        out.update(p=(1 + int(np.sum(np.abs(e @ s) >= obs - tol))) / (1 + draws), min_p=1.0 / (1 + draws),
                   exact=False)  # fmt: skip
    return out


def period_permutation_p(x1: Sequence[float], x2: Sequence[float], size: int, *,
                         min_blocks: int = specs.MIN_BLOCKS_B, exact_max: int = specs.EXACT_MAX_COMBOS,
                         draws: int = specs.DRAWS, seed: int = specs.SEED,
                         tol: float = specs.TOLERANCE) -> dict[str, Any]:  # fmt: skip
    """Family B's null: whole blocks are reassigned between the two periods. Blocks are formed inside each period;
    D = mean(period-1 block means) - mean(period-2 block means); p = the share of the C(q1 + q2, q1) reassignments
    with |D*| >= |D| - tol (all of them when at most `exact_max`, else `draws` random ones and p = (1 + count) /
    (1 + draws)). p is None unless each period has `min_blocks` blocks."""
    a1, a2 = np.asarray(x1, dtype=float), np.asarray(x2, dtype=float)
    b1 = [float(a1[s].mean()) for s in blocks(len(a1), size)]
    b2 = [float(a2[s].mean()) for s in blocks(len(a2), size)]
    q1, q2 = len(b1), len(b2)
    out: dict[str, Any] = {"p": None, "blocks": [q1, q2], "block": size, "difference": None, "min_p": None,
                           "exact": None}  # fmt: skip
    if q1 and q2:
        out["difference"] = float(np.mean(b1) - np.mean(b2))
    if min(q1, q2) < max(1, min_blocks):
        return out
    allb = np.array(b1 + b2)
    q, total, obs = q1 + q2, float(np.sum(b1) + np.sum(b2)), abs(out["difference"])

    def diff(s1: np.ndarray) -> np.ndarray:
        return np.abs(s1 / q1 - (total - s1) / q2)

    n = math.comb(q, q1)
    if n <= exact_max:
        idx = np.array(list(combinations(range(q), q1)))
        p = float(np.mean(diff(allb[idx].sum(axis=1)) >= obs - tol))
        out.update(p=p, min_p=(2.0 if q1 == q2 else 1.0) / n, exact=True)  # the mirror exists when q1 = q2
    else:
        rng = np.random.default_rng(seed)
        idx = rng.permuted(np.tile(np.arange(q), (draws, 1)), axis=1)[:, :q1]
        out.update(p=(1 + int(np.sum(diff(allb[idx].sum(axis=1)) >= obs - tol))) / (1 + draws),
                   min_p=1.0 / (1 + draws), exact=False)  # fmt: skip
    return out


# --------------------------------------------------------------------------------------------- multiple testing
def bh_qvalues[K: Hashable](pvals: Mapping[K, float | None], m: int | None = None) -> dict[K, float | None]:
    """Benjamini-Hochberg q-values: q_(i) = min over j >= i of m p_(j) / j, capped at 1. `m` is the registered family
    size (default: every key given, known or not): an unknown p keeps an unknown q and still counts in m."""
    m = len(pvals) if m is None else m
    known = sorted((p, i) for i, p in enumerate(pvals.values()) if p is not None)
    if m < len(pvals):
        raise ValueError("the family size cannot be smaller than the number of tests given")
    keys = list(pvals)
    out: dict[K, float | None] = dict.fromkeys(keys)
    running = 1.0
    for rank in range(len(known), 0, -1):
        p, i = known[rank - 1]
        running = min(running, m * p / rank)
        out[keys[i]] = running
    return out

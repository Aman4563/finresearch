"""The registered statistics of the seen-period risk-budget study (#395 Stage 2; riskbudget/specs.py STATISTICS):
calendar-month P&L paths from a book's daily marked equity, the worst month W, the maximum drawdown D (₹, the capital
as the first peak), the size-matched control, the paired one-sided moving-block bootstrap over months for W and D, and
the trades-taken test. Pure."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np

from finresearch.riskbudget import specs

Month = tuple[str, list[float]]  # ("YYYY-MM", that month's daily P&L in session order)
CHUNK = 500  # draws evaluated at once (memory only; the result does not depend on it)


def months(equity: Sequence[tuple[date, float]], capital: float) -> list[Month]:
    """Each calendar month's daily P&L: E(d) − E(the previous session), the capital before the first."""
    out: dict[str, list[float]] = {}
    prev = capital
    for d, e in equity:
        out.setdefault(f"{d:%Y-%m}", []).append(e - prev)
        prev = e
    return list(out.items())


def worst_month(ms: Sequence[Month]) -> float | None:
    return min(sum(p) for _, p in ms) if ms else None


def max_drawdown(daily: Sequence[float]) -> float:
    """The largest fall of the cumulated P&L from its running peak, the peak starting at 0 (the capital)."""
    cum = peak = dd = 0.0
    for x in daily:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def block_indices(m: int, block: int, draws: int, seed: int) -> np.ndarray:
    """Moving blocks (Künsch 1989): per draw ceil(m / block) blocks of `block` consecutive months, each start uniform
    over the m − block + 1 full starts, concatenated and cut to m months. Shape (draws, m)."""
    rng = np.random.default_rng(seed)
    nb = math.ceil(m / block)
    starts = rng.integers(0, m - block + 1, size=(draws, nb))
    return (starts[:, :, None] + np.arange(block)).reshape(draws, nb * block)[:, :m]


def _wd(ms: Sequence[Month], idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """W and D of every drawn month order (rows of `idx`); months are zero-padded to one width, which changes
    neither (a zero day moves no total, peak or fall)."""
    arr = np.zeros((len(ms), max(len(p) for _, p in ms)))
    for i, (_, p) in enumerate(ms):
        arr[i, : len(p)] = p
    w = arr.sum(axis=1)[idx].min(axis=1)
    cum = np.cumsum(arr[idx].reshape(idx.shape[0], -1), axis=1)
    return w, (np.maximum.accumulate(np.maximum(cum, 0.0), axis=1) - cum).max(axis=1)


def primary(b: Sequence[Month], c: Sequence[Month], *, block: int = specs.BLOCK_MONTHS, draws: int = specs.DRAWS,
            seed: int = specs.SEED, alpha: float = specs.ALPHA, min_months: int = specs.MIN_MONTHS) -> dict[str, Any]:  # fmt: skip
    """W_b > W_c and D_b < D_c, each by the paired one-sided moving-block bootstrap: p = the share of draws whose
    difference (ΔW = W_b − W_c, ΔD = D_c − D_b) is ≤ 0; both must be below `alpha`. Too few months: unknown, fails."""
    if [k for k, _ in b] != [k for k, _ in c]:
        raise ValueError("the two books must cover the same calendar months")
    flat = lambda ms: [x for _, p in ms for x in p]  # noqa: E731
    wb, wc, db, dc = worst_month(b), worst_month(c), max_drawdown(flat(b)), max_drawdown(flat(c))
    out: dict[str, Any] = {
        "months": len(b), "block_months": block, "draws": draws, "seed": seed, "alpha": alpha, "passes": False,
        "reason": None,
        "worst_month": {"budgeted": wb, "control": wc, "delta": None if wb is None else wb - wc, "p": None,
                        "passes": None},
        "max_drawdown": {"budgeted": db, "control": dc, "delta": dc - db, "p": None, "passes": None},
    }  # fmt: skip
    if len(b) < min_months:
        out["reason"] = f"only {len(b)} calendar months (needs {min_months}): unknown, which fails"
        return out
    idx, n_w, n_d = block_indices(len(b), block, draws, seed), 0, 0
    for lo in range(0, draws, CHUNK):
        (w_b, d_b), (w_c, d_c) = _wd(b, idx[lo : lo + CHUNK]), _wd(c, idx[lo : lo + CHUNK])
        n_w, n_d = n_w + int(np.sum(w_b - w_c <= 0)), n_d + int(np.sum(d_c - d_b <= 0))
    for key, n in (("worst_month", n_w), ("max_drawdown", n_d)):
        out[key]["p"] = n / draws
        out[key]["passes"] = n / draws < alpha
    out["passes"] = out["worst_month"]["passes"] and out["max_drawdown"]["passes"]
    return out


def control_k(
    risk_b: Sequence[tuple[date, float | None]], risk_u: Sequence[tuple[date, float | None]]
) -> float | None:
    """k = mean open risk of the budgeted book ÷ that of the uncapped book over every session; None when any open
    risk is unknown or the uncapped book never held any."""
    rb, ru = [r for _, r in risk_b], [r for _, r in risk_u]
    if not rb or not ru or any(r is None for r in (*rb, *ru)):
        return None
    mu = sum(ru) / len(ru)  # type: ignore[arg-type]
    return None if mu <= 0 else sum(rb) / len(rb) / mu  # type: ignore[arg-type]


def scaled(equity: Sequence[tuple[date, float]], k: float, capital: float) -> list[tuple[date, float]]:
    """The size-matched control: the uncapped book's P&L × k (fractional lots) on the same capital."""
    return [(d, capital + k * (e - capital)) for d, e in equity]


def secondary(entries_b: int, entries_u: int, max_fall: float = specs.MAX_TRADES_FALL) -> dict[str, Any]:
    fall = None if entries_u <= 0 else 1 - entries_b / entries_u
    return {"entries_budgeted": entries_b, "entries_uncapped": entries_u, "fall": fall, "max_fall": max_fall,
            "passes": fall is not None and fall <= max_fall}  # fmt: skip

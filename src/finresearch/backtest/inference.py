"""Inference recalibration of the F&O backtest (#381, P2.6). Pure functions; each returns None (unknown) where its
statistic is undefined, never 0. Criteria v2 (backtest/criteria_v2.py) uses them as a SHADOW verdict beside the
registered one; nothing here changes a registered status.

- Variance of an estimated per-period Sharpe ratio: Var(SR̂) = (1 − γ₃·SR + (γ₄−1)/4 · SR²)/(T−1), the denominator of
  the deflated Sharpe ratio (Bailey & López de Prado 2014, eq. 2; metrics.sr_variance). Under the null of no skill
  (SR = 0) it is 1/(T−1) whatever the skew and kurtosis: V_null.
- Deflated Sharpe v2: SR₀ from eq. 1 with V = V_null, V_cand (the sample variance of the trials' out-of-sample daily
  SRs) or max(V_null, V_cand), and N = the number of trials whose SR enters V_cand (not VARIANTS_TRIED).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from finresearch.backtest.metrics import deflated_sharpe, expected_max_sharpe, sr_variance

DSR_RULES = ("v_null", "v_cand", "max")
__all__ = ["DSR_RULES", "dsr_v2", "sr_variance"]


def _sample_var(xs: Sequence[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def dsr_v2(w: dict[str, Any], trial_srs: Sequence[float], rule: str = "max") -> dict[str, Any]:
    """DSR of one window `w` (daily_sr, days, skew, kurtosis as metrics.equity_stats gives them) against the trials'
    daily SRs under `rule` (module docstring). T = days − 1 daily returns."""
    if rule not in DSR_RULES:
        raise ValueError(f"unknown DSR rule {rule!r}; one of {DSR_RULES}")
    srs = [float(x) for x in trial_srs if x is not None]
    t = (w.get("days") or 0) - 1
    v_null = sr_variance(0.0, t, 0.0, 3.0)
    v_cand = _sample_var(srs)
    v = {"v_null": v_null, "v_cand": v_cand,
         "max": None if v_null is None or v_cand is None else max(v_null, v_cand)}[rule]  # fmt: skip
    out: dict[str, Any] = {"rule": rule, "n_trials": len(srs), "t": t, "v_null": v_null, "v_cand": v_cand,
                           "v_used": v, "sr0": None, "dsr": None}  # fmt: skip
    sr, sk, ku = w.get("daily_sr"), w.get("skew"), w.get("kurtosis")
    if v is None or sr is None or sk is None or ku is None or not srs:
        return out
    out["sr0"] = expected_max_sharpe(v, len(srs))
    out["dsr"] = deflated_sharpe(sr, t, sk, ku, len(srs), v)
    return out

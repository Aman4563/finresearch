"""How well a shown probability is supported (#218), so "96 %" is never read as proven accuracy.

Three separate facts, next to every forecast probability:
1. the base-rate sample: how many historical cases the probability (or its reference rate) rests on;
2. the method's validation status (`Signal.validation`: backtested, base rate, rule-based, uncalibrated, shadow);
3. its calibration from the forecast ledger: how many of this method's own forecasts have resolved and been scored,
   counted as independent events (`ledger.independent_events`). Calibration is "not established" until the
   calibration policy's first recalibration tier ("shrink", 50 independent outcomes; evals/calibration_policy.py
   and evals/experiments/calibration_policy/PREREG.md) is reached. Below that, a reliability diagram is noise.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import Forecast
from finresearch.evals.calibration_policy import TIERS, policy_tier

CALIBRATION_MIN_N = TIERS[1][1]  # 50: the first tier that recalibrates (shrink)
VALIDATION_LABEL = {
    "backtested": "backtested out of sample",
    "base_rate": "historical base rate, no fitted model",
    "rule_based": "rule-based, not validated on outcomes",
    "uncalibrated": "uncalibrated",
    "shadow": "shadow test, not used for the call",
}
CASES = {"ipo": "past IPOs"}  # noun for the base-rate sample; others read "comparable past cases"


def scored_events(session: Session, asset: str, method: str) -> int:
    """Independent resolved outcomes of this (asset, method) group that carried a probability."""
    from finresearch.signals.ledger import _method_group, independent_events

    group = _method_group(method)
    rows = session.scalars(select(Forecast).where(Forecast.asset == asset, Forecast.status == "resolved",
                                                  Forecast.probability.isnot(None), Forecast.outcome.isnot(None)))  # fmt: skip
    return len(independent_events([f for f in rows if _method_group(f.method) == group]))


def _pct(p: float) -> str:
    return f"{p * 100:.0f} %"


def reliability(sig: dict[str, Any], scored: int) -> dict[str, Any]:
    """The support for `sig` (a `Signal.to_json()`), given `scored` independent scored outcomes of its method."""
    br = sig.get("base_rate") or {}
    val = sig.get("validation") or {}
    status = val.get("status") or "uncalibrated"
    n_base = br.get("n") if br.get("n") is not None else (val.get("n") or None) if status in ("base_rate", "backtested") else None  # fmt: skip
    established = scored >= CALIBRATION_MIN_N
    calibration = {
        "scored": scored,
        "established": established,
        "tier": policy_tier(scored),
        "needed": CALIBRATION_MIN_N,
        "label": (f"calibration established: {scored} scored" if established
                  else f"calibration not established: {scored} scored"),
    }  # fmt: skip
    noun = CASES.get(sig.get("asset") or "", "comparable past cases")
    support = [
        f"base rate from {n_base} {noun}" if n_base else "no historical base rate",
        calibration["label"],
    ]
    p, iv = sig.get("probability"), sig.get("probability_interval")
    shown = (
        [_pct(p) + (f" (range {iv[0] * 100:.0f}–{iv[1] * 100:.0f} %)" if iv else "")] if p is not None else []
    )
    return {
        "base_rate_n": n_base,
        "validation": {"status": status, "n": val.get("n") or 0, "label": VALIDATION_LABEL.get(status, status)},
        "calibration": calibration,
        "support": " · ".join(support),  # shown beside the probability
        "summary": " · ".join([*shown, *support]),
        "how": (f"Base-rate n = historical cases behind the estimate. Calibration counts this method's own resolved "
                f"forecasts as independent events (one per IPO listing; non-overlapping windows per stock); it is "
                f"established at {CALIBRATION_MIN_N}, the calibration policy's first recalibration tier."),
    }  # fmt: skip

"""Switch criterion for the IPO blend's live shadow test (pre-registered in evals/experiments/ipo_calibration/SHADOW.md).

Pairs: mainboard IPOs where the base-rate call and the shadow blend both logged a resolved forecast; per method the
latest forecast counts. d_i = (p_table − y)² − (p_blend − y)² (> 0: the blend was better). Switch only when n ≥ 30,
mean d > 0 and the lower end of a paired-bootstrap 90 % interval (10,000 resamples, seed 20261001) is > 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

MIN_PAIRS = 30
RETIRE_AT = 60
RESAMPLES = 10_000
SEED = 20261001


@dataclass(frozen=True)
class Pair:
    instrument: str
    p_table: float
    p_blend: float
    y: int


def shadow_verdict(pairs: Sequence[Pair], *, min_pairs: int = MIN_PAIRS, resamples: int = RESAMPLES,
                   seed: int = SEED) -> dict[str, Any]:  # fmt: skip
    n = len(pairs)
    out: dict[str, Any] = {"n": n, "min_pairs": min_pairs, "switch": False}
    if n == 0:
        return {**out, "reason": "no resolved pairs yet"}
    y = np.asarray([p.y for p in pairs], dtype=float)
    d = (np.asarray([p.p_table for p in pairs]) - y) ** 2 - (np.asarray([p.p_blend for p in pairs]) - y) ** 2
    mean = float(d.mean())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(resamples, n))
    boot = d[idx].mean(axis=1)
    lo, hi = float(np.percentile(boot, 5)), float(np.percentile(boot, 95))
    out.update(brier_table=float(((np.asarray([p.p_table for p in pairs]) - y) ** 2).mean()),
               brier_blend=float(((np.asarray([p.p_blend for p in pairs]) - y) ** 2).mean()),
               mean_diff=mean, ci90=[lo, hi])  # fmt: skip
    if n < min_pairs:
        return {**out, "reason": f"{n} of {min_pairs} pairs: not evaluated yet"}
    if mean > 0 and lo > 0:
        return {**out, "switch": True, "reason": "the blend's Brier is lower and the 90% interval excludes 0"}
    retire = n >= RETIRE_AT
    return {
        **out,
        "retire": retire,
        "reason": "keep the table" + ("; retire the shadow (n ≥ 60)" if retire else ""),
    }


def pairs_from_ledger(session: Any, table_method: str, blend_method: str) -> list[Pair]:
    """Resolved IPO listing-gain forecasts of both methods for the same instrument and listing date; the latest
    forecast per method counts. Every blend row is a shadow row: row 253 (named in SHADOW.md) or logged after it."""
    from sqlalchemy import select

    from finresearch.db.models import Forecast

    latest: dict[tuple[str, str, Any], Forecast] = {}
    q = select(Forecast).where(Forecast.asset == "ipo", Forecast.event_kind == "listing_gain",
                               Forecast.status == "resolved", Forecast.probability.isnot(None),
                               Forecast.method.in_([table_method, blend_method])).order_by(Forecast.created_at)  # fmt: skip
    for f in session.scalars(q):
        latest[(f.method, f.instrument, f.resolve_on)] = f
    out = []
    for (method, inst, day), t in latest.items():
        if method != table_method:
            continue
        b = latest.get((blend_method, inst, day))
        if b is not None and t.outcome is not None:
            out.append(Pair(inst, float(t.probability), float(b.probability), int(t.outcome)))
    return out

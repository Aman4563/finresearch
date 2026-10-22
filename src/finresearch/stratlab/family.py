"""The Strategy Lab's trial family (#380, P2.5): every spec ever run, tested together for selection bias.

`matrix` builds the T × N P&L matrix from the trial log (stratlab.trials): per spec hash its longest done trial with a
daily vector (in sample or out of sample; a later, longer replay of the same spec replaces a shorter one), on the
sessions that EVERY such trial covers. A day a trial did not replay is unknown, so it is dropped from the grid, never
filled with 0. Trials without a vector (failed, timed out, logged before the vector was kept) still count in N
(store.n_trials) and are listed in `without_vector`.

`statistics` runs PBO (CSCV), Hansen's SPA (benchmark: not trading, P&L 0) and the Romano-Wolf stepdown with the
registered settings (stratlab.rigour.FAMILY). Fewer common sessions than FAMILY.min_grid_days, or fewer than two
specs: every statistic is unknown, and the promotion gate reads unknown as a fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import LabTrial
from finresearch.stratlab import overfit as of
from finresearch.stratlab.rigour import FAMILY, Family


@dataclass
class Matrix:
    hashes: list[str]
    days: list[str]
    values: np.ndarray  # len(days) × len(hashes), ₹ P&L per session
    without_vector: list[str]


def matrix(s: Session) -> Matrix:
    best: dict[str, list[list[Any]]] = {}
    seen: list[str] = []
    for t in s.scalars(select(LabTrial).order_by(LabTrial.id)):
        if t.spec_hash not in seen:
            seen.append(t.spec_hash)
        if t.status == "done" and t.daily and len(t.daily) >= len(best.get(t.spec_hash, [])):
            best[t.spec_hash] = t.daily
    hashes = [h for h in seen if h in best]
    common: set[str] | None = None
    for h in hashes:
        days = {d for d, _ in best[h]}
        common = days if common is None else common & days
    grid = sorted(common or set())
    cols = []
    for h in hashes:
        by_day = {d: float(x) for d, x in best[h]}
        cols.append([by_day[d] for d in grid])
    values = np.array(cols, dtype=float).T if hashes and grid else np.empty((len(grid), len(hashes)))
    return Matrix(hashes, grid, values, [h for h in seen if h not in best])


def statistics(m: Matrix, fam: Family = FAMILY) -> dict[str, Any]:
    t, n = m.values.shape
    out: dict[str, Any] = {"specs": n, "days": t, "from": m.days[0] if m.days else None,
                           "to": m.days[-1] if m.days else None, "without_vector": m.without_vector,
                           "settings": {"blocks": fam.pbo_blocks, "stat": fam.pbo_stat, "draws": fam.bootstrap_draws,
                                        "mean_block": fam.mean_block, "seed": fam.seed, "rw_alpha": fam.rw_alpha},
                           "note": None}  # fmt: skip
    if t < fam.min_grid_days or n < 2:
        out["note"] = (f"{t} common sessions and {n} specs with a daily P&L: fewer than {fam.min_grid_days} sessions "
                       "or 2 specs, so PBO, SPA and Romano-Wolf are unknown (unknown fails the gate)")  # fmt: skip
        out["pbo"] = {"pbo": None}
        out["spa"] = {"p_consistent": None}
        out["romano_wolf"] = {"rejected": None, "rejected_hashes": None}
        return out
    out["pbo"] = {
        k: v for k, v in of.pbo_cscv(m.values, s=fam.pbo_blocks, stat=fam.pbo_stat).items() if k != "logits"
    }
    out["pbo"]["excluded"] = [m.hashes[j] for j in out["pbo"]["excluded"]]
    idx = of.sb_indices(
        t, fam.bootstrap_draws, mean_block=fam.mean_block, rng=np.random.default_rng(fam.seed)
    )
    q = 1.0 / fam.mean_block
    out["spa"] = of.spa(m.values, idx, q=q)
    rw = of.romano_wolf(m.values, idx, q=q, alpha=fam.rw_alpha)
    rw["rejected_hashes"] = None if rw["rejected"] is None else [m.hashes[j] for j in rw["rejected"]]
    out["romano_wolf"] = rw
    return out

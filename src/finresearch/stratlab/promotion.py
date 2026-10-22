"""Forward incubation, the Strategy Lab's only promotion path (#380, P2.5). `checks` is the gate; it is the only code
that can write the passing status (tests/test_lab_promotion.py checks the literal appears nowhere else). Every check
must hold, and an unknown value fails (unknown is never a pass):

1. Registration: stratlab.rigour hashes to rigour_prereg.HASH (the gate's own thresholds are the registered ones).
2. Forward holdout: the latest out-of-sample run of the version reads "candidate" on its sealed holdout (sessions after
   the spec's registration date; stratlab.evaluate.holdout).
3. PBO < GATE.max_pbo and 4. Hansen's consistent SPA p < GATE.max_spa_p, on the whole trial family (stratlab.family).
5. Forward paper trades >= the library's min_oos_trades and 6. their expiry-clustered t >= its min_t_stat: closed
   paper trades of the version's paper rule sets whose signal came after the registration date (stratlab.paper).
7. MinTRL: the paper record has at least MinTRL trades (Bailey & López de Prado 2012, eq. 11, per trade: SR = mean /
   sample sd, skewness and raw kurtosis of the trades' net P&L, SR* = GATE.min_trl_sr_star).

Passing lifts "no proven edge" and nothing else: it places no order and sizes nothing (brokers are read-only).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.backtest import metrics
from finresearch.stratlab import overfit, rigour, rigour_prereg
from finresearch.stratlab.rigour import GATE

PASSED = "incubation_passed"
NOT_PASSED = "no_proven_edge"


def _c(name: str, value: Any, ok: bool, rule: str) -> dict[str, Any]:
    return {"criterion": name, "value": value, "pass": bool(ok),
            "rule": rule + ("; unknown (fails)" if value is None else "")}  # fmt: skip


def _moments(x: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    """Per-trade SR (mean / sample sd), skewness and raw kurtosis (population moments, as metrics.equity_stats)."""
    a = np.asarray(x, dtype=float)
    if len(a) < 2 or float(a.std(ddof=1)) <= 0:
        return None, None, None
    z = (a - a.mean()) / float(a.std(ddof=0))
    return float(a.mean() / a.std(ddof=1)), float(np.mean(z**3)), float(np.mean(z**4))


def checks(*, holdout: str | None, pbo: float | None, spa_p: float | None, nets: Sequence[float],
           expiries: Sequence[date], registered: bool) -> dict[str, Any]:  # fmt: skip
    sr, sk, ku = _moments(nets)
    need = overfit.min_trl(sr, sk, ku, sr_star=GATE.min_trl_sr_star, alpha=GATE.min_trl_alpha)
    t = metrics.clustered_t(list(nets), list(expiries)) if len(nets) >= 2 else None
    out = [
        _c("Registration", rigour_prereg.HASH[:12] if registered else None, registered,
           "stratlab/rigour.py hashes to rigour_prereg.HASH"),
        _c("Forward holdout", holdout, holdout == "candidate", "candidate on the sessions after registration"),
        _c("PBO", pbo, pbo is not None and pbo < GATE.max_pbo, f"< {GATE.max_pbo:g}"),
        _c("SPA p (consistent)", spa_p, spa_p is not None and spa_p < GATE.max_spa_p, f"< {GATE.max_spa_p:g}"),
        _c("Forward paper trades", len(nets), len(nets) >= GATE.min_paper_trades, f">= {GATE.min_paper_trades}"),
        _c("Forward paper t-stat (expiry-clustered)", t, t is not None and t >= GATE.min_paper_t,
           f">= {GATE.min_paper_t:g}"),
        _c("MinTRL (trades)", need, need is not None and len(nets) >= need,
           f"the paper record ({len(nets)} trades) at least this long"),
    ]  # fmt: skip
    return {"status": PASSED if all(c["pass"] for c in out) else NOT_PASSED, "checks": out}


def paper_record(s: Session, version_id: int, after: date | None) -> tuple[list[float], list[date]]:
    """Net P&L and expiry of the version's closed paper trades whose signal came after `after` (None: none count)."""
    from finresearch.db.models import LabPaperRule, LabPaperSignal, PaperStrategy

    if after is None:
        return [], []
    rows = s.execute(
        select(PaperStrategy.net_pnl, PaperStrategy.expiry)
        .join(LabPaperSignal, LabPaperSignal.paper_id == PaperStrategy.id)
        .join(LabPaperRule, LabPaperRule.id == LabPaperSignal.rule_id)
        .where(LabPaperRule.version_id == version_id, LabPaperSignal.day > after,
               PaperStrategy.status == "closed", PaperStrategy.net_pnl.is_not(None))
        .order_by(LabPaperSignal.day)
    ).all()  # fmt: skip
    return [float(n) for n, _ in rows], [e for _, e in rows]


def judge(s: Session, version_id: int, family_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """The gate for one version, from the database (reads only)."""
    from finresearch.db.models import LabRun, LabVersion
    from finresearch.stratlab import family, store

    v = s.get(LabVersion, version_id)
    if v is None:
        raise store.LabError(f"no lab version {version_id}")
    reg = store.registered_on(s, v.spec_hash)
    run = s.scalars(select(LabRun).where(LabRun.version_id == version_id, LabRun.oos.is_(True),
                                         LabRun.status == "done").order_by(LabRun.id.desc())).first()  # fmt: skip
    hold = ((run.result or {}).get("holdout") or {}).get("status") if run is not None else None
    fam = family_stats if family_stats is not None else family.statistics(family.matrix(s))
    nets, exps = paper_record(s, version_id, reg)
    got = checks(holdout=hold, pbo=(fam.get("pbo") or {}).get("pbo"), spa_p=(fam.get("spa") or {}).get("p_consistent"),
                 nets=nets, expiries=exps, registered=rigour.registration_hash() == rigour_prereg.HASH)  # fmt: skip
    return {**got, "version_id": version_id, "spec_hash": v.spec_hash,
            "registered_on": reg.isoformat() if reg else None, "holdout_run_id": run.id if run else None,
            "family": {k: fam.get(k) for k in ("specs", "days", "from", "to", "note", "without_vector")}}  # fmt: skip

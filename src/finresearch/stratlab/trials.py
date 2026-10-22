"""The Strategy Lab's trial log (#380, P2.5): every run that started is one append-only `lab_trial` row (in sample,
out of sample or forward holdout; done, no_data, failed or timed out), written in the same transaction that gives the
run its final status (stratlab.jobs), so a run can never end without counting. The rows feed:

- the trial count N of the deflated Sharpe (stratlab.store.n_trials): VARIANTS_TRIED + every distinct spec hash ever
  run in the lab + every vault read that counts in N (evals/experiments/vault/PREREG.md §2.5);
- the trial matrix of PBO, SPA and Romano-Wolf (`matrix`): each done trial's daily P&L on a common day grid.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import LabRun, LabStrategy, LabTrial, LabVersion


def daily_pnl(equity: list[tuple[date, float]] | list[list[Any]], capital: float) -> list[list[Any]]:
    """[[ISO day, ₹ P&L]] from the engine's marked equity: each day's change, the first day against the capital."""
    out, prev = [], round(capital, 2)
    for d, v in equity:
        cur = round(
            float(v), 2
        )  # differences of rounded levels: the vector adds up to the equity change exactly
        out.append([d.isoformat() if isinstance(d, date) else str(d), round(cur - prev, 2)])
        prev = cur
    return out


def _git_sha() -> str | None:
    try:
        got = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, capture_output=True, text=True,
                             timeout=5, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    sha = got.stdout.strip()
    return sha if got.returncode == 0 and len(sha) == 40 else None


def log(
    s: Session, run: LabRun, status: str, daily: list[list[Any]] | None = None, mode: str | None = None
) -> LabTrial:
    """The trial row of `run` (one per run: lab_trial.run_id is unique)."""
    v = s.get(LabVersion, run.version_id)
    st = s.get(LabStrategy, v.strategy_id)
    daily = daily or []
    t = LabTrial(run_id=run.id, version_id=v.id, spec_hash=v.spec_hash, underlying=st.underlying,
                 mode=mode or ("oos" if run.oos else "is"), status=status[:12],
                 first_day=date.fromisoformat(daily[0][0]) if daily else None,
                 last_day=date.fromisoformat(daily[-1][0]) if daily else None, git_sha=_git_sha(), daily=daily)  # fmt: skip
    s.add(t)
    s.flush()
    return t


def lab_hashes(s: Session) -> set[str]:
    """Every spec hash ever run in the lab: the trial log, plus the variants counter (runs before the log existed)."""
    from finresearch.db.models import LabVariant

    return set(s.scalars(select(LabTrial.spec_hash))) | set(s.scalars(select(LabVariant.spec_hash)))

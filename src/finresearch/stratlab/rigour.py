"""The registration of the Strategy Lab's rigour rules (#380, P2.5): the promotion gate, the trial-family statistics and
the pre-registered null experiment. evals/experiments/lab_rigour/PREREG.md describes them; `rigour_prereg.HASH` pins
`registration_hash()` and stratlab.promotion refuses to judge (status unknown) when they differ. Never edit a value
here in place: a change is a new registration with its own hash, recorded in the experiment's ADDENDUM.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Family:
    """How the trial family is tested (stratlab.family)."""

    pbo_blocks: int = 16  # S: C(16, 8) = 12,870 halves (Bailey et al. 2017)
    pbo_stat: str = "sharpe"
    bootstrap_draws: int = 1000
    mean_block: float = 10.0  # sessions: the stationary bootstrap's q = 1/10
    seed: int = 380
    rw_alpha: float = 0.05
    min_grid_days: int = 64  # fewer common days than this: PBO, SPA and Romano-Wolf are unknown


@dataclass(frozen=True)
class Gate:
    """All must hold for 'incubation passed', the only status that lifts 'no proven edge' (stratlab.promotion)."""

    max_pbo: float = 0.2
    max_spa_p: float = 0.05  # Hansen's consistent p-value
    min_paper_trades: int = 30  # the library's min_oos_trades (backtest.specs.CRITERIA)
    min_paper_t: float = 2.0  # the library's min_t_stat, expiry-clustered
    min_trl_alpha: float = 0.05  # MinTRL (Bailey & López de Prado 2012) on the per-trade P&L, SR* = 0
    min_trl_sr_star: float = 0.0


@dataclass(frozen=True)
class NullSim:
    """The pre-registered null experiment (PREREG.md §4), synthetic data only."""

    noise_panels: int = 200
    noise_n: int = 300
    noise_t: int = 1000
    planted_panels: int = 200
    planted_noise_n: int = 50
    planted_t: tuple[int, ...] = (1000, 3400)  # primary 1,000 (the noise panel's T); 3,400 reported beside it
    planted_annual_sr: float = 1.5
    incubation_runs: int = 1000
    incubation_days: int = 3400
    seed: int = 380


FAMILY, GATE, NULL_SIM = Family(), Gate(), NullSim()


def registration_hash() -> str:
    body = {"family": asdict(FAMILY), "gate": asdict(GATE), "null_sim": asdict(NULL_SIM)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

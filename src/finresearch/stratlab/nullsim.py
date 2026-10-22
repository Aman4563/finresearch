"""The pre-registered null experiment of the Lab's rigour rules (#380, evals/experiments/lab_rigour/PREREG.md §5).
Synthetic data only (iid standard-normal daily P&L; a planted trial adds its daily Sharpe to the mean), every panel
seeded from (NULL_SIM.seed, item, panel), the family statistics with the registered settings (rigour.FAMILY).

A. noise PBO: mean PBO over the noise panels, registered range [0.45, 0.55].
B. noise SPA: share of noise panels with SPA consistent p < GATE.max_spa_p, registered at most 5 %.
C. planted: one trial with the planted annual SR among the noise trials, per T: mean PBO < GATE.max_pbo and SPA
   rejecting in at least 80 % of panels.
D. incubation power: one planted trial over NULL_SIM.incubation_days sessions; share of runs whose length meets
   MinTRL(SR̂, γ̂₃, γ̂₄), registered at least 70 %.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import numpy as np

from finresearch.stratlab import overfit as of
from finresearch.stratlab.rigour import FAMILY, GATE, NULL_SIM, Family, NullSim, registration_hash


def rng(seed: int, item: str, panel: int) -> np.random.Generator:
    return np.random.default_rng([seed, zlib.crc32(item.encode()), panel])


def noise_panel(g: np.random.Generator, n: int, t: int) -> np.ndarray:
    return g.standard_normal((t, n))


def planted_panel(g: np.random.Generator, n_noise: int, t: int, daily_sr: float) -> np.ndarray:
    """Column 0 has mean `daily_sr` (sd 1, so its Sharpe is daily_sr); columns 1..n_noise are noise."""
    m = g.standard_normal((t, n_noise + 1))
    m[:, 0] += daily_sr
    return m


def panel(m: np.ndarray, g: np.random.Generator, fam: Family) -> tuple[float | None, float | None]:
    """(PBO, SPA consistent p) of one panel."""
    pbo = of.pbo_cscv(m, s=fam.pbo_blocks, stat=fam.pbo_stat)["pbo"]
    idx = of.sb_indices(m.shape[0], fam.bootstrap_draws, mean_block=fam.mean_block, rng=g)
    return pbo, of.spa(m, idx, q=1.0 / fam.mean_block)["p_consistent"]


def _summary(rows: list[tuple[float | None, float | None]]) -> dict[str, Any]:
    pbos = [p for p, _ in rows if p is not None]
    ps = [p for _, p in rows if p is not None]
    return {"panels": len(rows), "unknown": sum(p is None or q is None for p, q in rows),
            "mean_pbo": float(np.mean(pbos)) if pbos else None,
            "spa_reject_rate": sum(p < GATE.max_spa_p for p in ps) / len(rows) if rows else None}  # fmt: skip


def run(cfg: NullSim = NULL_SIM, fam: Family = FAMILY,
        progress: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:  # fmt: skip
    daily = cfg.planted_annual_sr / math.sqrt(252)
    items: dict[str, Any] = {}
    noise = []
    for i in range(cfg.noise_panels):
        g = rng(cfg.seed, "noise", i)
        noise.append(panel(noise_panel(g, cfg.noise_n, cfg.noise_t), g, fam))
        if progress:
            progress("noise", i + 1, cfg.noise_panels)
    a = _summary(noise)
    items["A_noise_pbo"] = {**a, "criterion": "mean PBO in [0.45, 0.55]",
                            "pass": a["mean_pbo"] is not None and 0.45 <= a["mean_pbo"] <= 0.55}  # fmt: skip
    items["B_noise_spa"] = {**a, "criterion": f"SPA p < {GATE.max_spa_p:g} in at most 5 % of panels",
                            "pass": a["spa_reject_rate"] is not None and a["spa_reject_rate"] <= 0.05}  # fmt: skip
    for t in cfg.planted_t:
        rows = []
        for i in range(cfg.planted_panels):
            g = rng(cfg.seed, f"planted{t}", i)
            rows.append(panel(planted_panel(g, cfg.planted_noise_n, t, daily), g, fam))
            if progress:
                progress(f"planted T={t}", i + 1, cfg.planted_panels)
        c = _summary(rows)
        ok = c["mean_pbo"] is not None and c["mean_pbo"] < GATE.max_pbo and (c["spa_reject_rate"] or 0) >= 0.8
        items[f"C_planted_t{t}"] = {**c, "t": t, "criterion": f"mean PBO < {GATE.max_pbo:g} and SPA rejects in "
                                    ">= 80 % of panels", "pass": ok}  # fmt: skip
    met = 0
    for i in range(cfg.incubation_runs):
        x = rng(cfg.seed, "incubation", i).standard_normal(cfg.incubation_days) + daily
        z = (x - x.mean()) / x.std(ddof=0)
        need = of.min_trl(float(x.mean() / x.std(ddof=1)), float(np.mean(z**3)), float(np.mean(z**4)),
                          sr_star=GATE.min_trl_sr_star, alpha=GATE.min_trl_alpha)  # fmt: skip
        met += need is not None and len(x) >= need
    power = met / cfg.incubation_runs if cfg.incubation_runs else None
    items["D_incubation_power"] = {"runs": cfg.incubation_runs, "days": cfg.incubation_days, "power": power,
                                   "criterion": "MinTRL met in >= 70 % of runs",
                                   "pass": power is not None and power >= 0.7}  # fmt: skip
    return {
        "registration_hash": registration_hash(),
        "planted_daily_sr": daily,
        "settings": {"family": asdict(fam), "gate": asdict(GATE), "null_sim": asdict(cfg)},
        "items": items,
    }

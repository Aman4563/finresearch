"""Tail-aware physical (real-world) distributions for an option structure's P&L at expiry (P3.4, #387).

Two models, both scaled to the option market's ATM IV so that only the SHAPE comes from history:
  M2  IV-scaled empirical terminal model: z_i = [ln(S_T,i / F_i) + sigma_i^2 T_i / 2] / (sigma_i sqrt(T_i)) per past
      expiry (sigma_i the ATM IV at entry); a new trade has S_T = F exp(-kappa sigma^2 T / 2 + sigma sqrt(T) z*) with
      kappa = E[z^2]; the point estimate is exact on the empirical z distribution, the EV interval comes from a
      stationary block bootstrap over expiries (Politis and Romano 1994, JASA 89, 1303-1313); an optional GPD
      peaks-over-threshold tail (Pickands 1975; McNeil and Frey 2000, J. Empirical Finance 7, 271-300).
  M3  filtered historical simulation (Barone-Adesi, Giannopoulos and Vosper 1999, J. Futures Markets 19, 583-602) on a
      GJR-GARCH(1,1) (Glosten, Jagannathan and Runkle 1993, J. Finance 48, 1779-1801) or EWMA (RiskMetrics 1996,
      lambda 0.94) filter, rescaled so that E[sum r^2] = kappa sigma_IV^2 T.

Plus: expected shortfall on a weighted sample, the robust EV gate (maxmin over validated models, Gilboa and
Schmeidler 1989, J. Math. Econ. 18, 141-153; never more permissive than the gate it wraps), drawdown by block
bootstrap (DaR/CDaR: Chekhlov, Uryasev and Zabarankin 2005, IJTAF 8, 13-58), VRP and Kelly with estimation error.
Library only: the live card's PoP/EV and gates are unchanged (they change only through the horse race, #377).
Every registered number is a module constant, hashed by horse-race addendum A5.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from finresearch.fincalc import options as o

DOTS = [(i + 0.5) / 20 for i in range(20)]
ES_ALPHA = 0.025  # ES97.5 (the issue's; Basel FRTB's level)


# ----------------------------------------------------------------------------------------------- the z statistic
def terminal_z(settle: float | None, fwd: float | None, sigma: float | None, t: float | None) -> float | None:
    """z = [ln(S_T / F) + sigma^2 T / 2] / (sigma sqrt(T)); None when any input is unknown or not positive."""
    if not settle or not fwd or not sigma or not t or settle <= 0 or fwd <= 0 or sigma <= 0 or t <= 0:
        return None
    return (math.log(settle / fwd) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))


def kappa(zs: Sequence[float]) -> float | None:
    """kappa = E[z^2] (1 for a lognormal at the right IV); None on an empty sample."""
    return float(np.mean(np.square(zs))) if len(zs) else None


def iv_scaled_terminal(fwd: float, sigma: float, t: float, z: np.ndarray, kap: float) -> np.ndarray:
    """S_T = F exp(-kappa sigma^2 T / 2 + sigma sqrt(T) z)."""
    return fwd * np.exp(-0.5 * kap * sigma * sigma * t + sigma * math.sqrt(t) * np.asarray(z, float))


# ------------------------------------------------------------------------------------------- expected shortfall
def expected_shortfall(x: Sequence[float], w: Sequence[float] | None = None, alpha: float = ES_ALPHA) -> float:
    """ES_alpha as a positive loss: -(1/alpha)[sum_{i<k} w_i x_(i) + (alpha - sum_{i<k} w_i) x_(k)] on the sorted
    values, k the first index whose cumulative weight reaches alpha (weights normalised to 1)."""
    xs = np.asarray(x, float)
    ws = np.full(len(xs), 1.0 / len(xs)) if w is None else np.asarray(w, float) / float(np.sum(w))
    order = np.argsort(xs, kind="stable")
    xs, ws = xs[order], ws[order]
    acc = total = 0.0
    for xi, wi in zip(xs, ws, strict=True):
        take = min(wi, alpha - acc)
        total += take * xi
        acc += take
        if acc >= alpha - 1e-15:
            break
    return -total / alpha


# --------------------------------------------------------------------------- outcome from a weighted sample
@dataclass(frozen=True)
class SampleOutcome:
    pop: float  # P(P&L after costs > 0)
    ev: float  # DF x E[gross] - net premium - entry costs - DF x E[exit costs] (fincalc.options.outcome's)
    dots: list[float]  # the 20 P&L quantiles at (i + 0.5) / 20 (fincalc.options.weighted_quantiles)
    es: float  # ES97.5 of the undiscounted P&L, a positive loss
    n: int


def sample_outcome(legs: Sequence[o.Leg], s_t: Sequence[float], t: float, rate: float, entry_cost: float = 0.0,
                   exit_cost: Callable[[float], float] | None = None,
                   weights: Sequence[float] | None = None) -> SampleOutcome:  # fmt: skip
    """PoP, EV, dots and ES of holding `legs` to expiry over terminal prices `s_t` with `weights` (equal if None),
    on fincalc.options.outcome's conventions so that a lognormal grid reproduces it."""
    st = np.asarray(s_t, float)
    w = np.full(len(st), 1.0 / len(st)) if weights is None else np.asarray(weights, float) / float(np.sum(weights))
    net = sum(x.premium * x.qty for x in legs if x.right != "future")
    gross = np.zeros(len(st))
    for x in legs:
        if x.right == "future":
            gross += x.qty * (st - x.strike)
        else:
            gross += x.qty * (np.maximum(st - x.strike, 0) if x.right == "call" else np.maximum(x.strike - st, 0))
    xc = np.array([exit_cost(float(p)) for p in st]) if exit_cost else np.zeros(len(st))
    pnl = gross - net - entry_cost - xc
    df = math.exp(-rate * t)
    ev = df * float(w @ gross) - net - entry_cost - df * float(w @ xc)
    dist = list(zip(pnl.tolist(), w.tolist(), strict=True))
    return SampleOutcome(float(w[pnl > 0].sum()), ev, o.weighted_quantiles(dist, DOTS),
                         expected_shortfall(pnl, w), len(st))  # fmt: skip

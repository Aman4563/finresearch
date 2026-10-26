"""Control charts over daily data-quality series (#368, P1.6): the small, lasting shifts a per-day check cannot see (a
parser that starts dropping a few percent of contracts, a forward or rate change that biases IV30 by a vol point, a
run of settles that barely moved). Non-critical: a chart says "this series left its range", not which value is wrong.

Baseline: the BASE (40) sessions before the last RUN (20), centre = median and σ = 1.4826 × MAD (robust, as
dq.stats). The chart runs over the last RUN sessions from the centre, on u = (x − centre) / σ. Fewer than BASE + RUN
sessions, or a flat baseline (MAD 0), is unknown, never ok.

* EWMA (Roberts 1959): z_i = λ u_i + (1 − λ) z_(i−1), out of control when |z| > L √(λ / (2 − λ)) (the asymptotic
  limit). λ = 0.2, L = 2.962: an in-control average run length of 500 sessions for independent normal data (Lucas &
  Saccucci 1990, Technometrics 32(1); reproduced for this module with the Brook & Evans 1972 Markov chain).
* Tabular CUSUM (Page 1954): C+_i = max(0, C+_(i−1) + u_i − k), C−_i = max(0, C−_(i−1) − u_i − k), out of control
  when either exceeds h. k = 0.5, h = 5: a two-sided in-control average run length of 465 (Montgomery, Introduction to
  Statistical Quality Control, Table 9.3; Siegmund's approximation and the Brook & Evans chain agree).

Event semantics: a chart warns on the session its statistic crosses the limit (inside on the session before). While
it stays beyond, later sessions are ok with "beyond its limit since D" in the note and the statistic in `value`, so a
lasting shift (a new contract cycle, a volatility regime) is reported once, not every day; it re-arms once back inside
(or once the baseline has absorbed the new level). These series are autocorrelated and move with the market, so the
run lengths are a scale, not a promise: the owner's replay (`finresearch dq replay`) measures the real warning rates.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from finresearch.dq.stats import MAD_SCALE

BASE, RUN = 40, 20
LAMBDA, L_EWMA = 0.2, 2.962
K_CUSUM, H_CUSUM = 0.5, 5.0


@dataclass(frozen=True)
class Chart:
    status: str  # ok / warn / unknown
    stat: float | None = None  # EWMA z, or the larger CUSUM side, on the last session
    limit: float | None = None
    centre: float | None = None
    sigma: float | None = None
    beyond_since: int | None = (
        None  # run index (0 = the first of the RUN sessions) where the current excursion began
    )
    note: str | None = None


def chart(kind: str, values: Sequence[float]) -> Chart:
    """`kind` ewma | cusum over `values` (oldest first; the last is the session being checked)."""
    if len(values) < BASE + RUN:
        return Chart("unknown", note=f"needs {BASE + RUN} sessions, has {len(values)}")
    xs = list(values)[-(BASE + RUN) :]
    base, run = xs[:BASE], xs[BASE:]
    centre = statistics.median(base)
    sigma = MAD_SCALE * statistics.median([abs(x - centre) for x in base])
    if not sigma > 0 or not math.isfinite(sigma):
        return Chart("unknown", centre=centre, note="the baseline is flat (MAD 0): no scale to chart against")
    path = []
    if kind == "ewma":
        limit, z = L_EWMA * math.sqrt(LAMBDA / (2 - LAMBDA)), 0.0
        for x in run:
            z = LAMBDA * (x - centre) / sigma + (1 - LAMBDA) * z
            path.append(z)
        out = [abs(z) > limit for z in path]
    elif kind == "cusum":
        limit, hi, lo = H_CUSUM, 0.0, 0.0
        for x in run:
            u = (x - centre) / sigma
            hi, lo = max(0.0, hi + u - K_CUSUM), max(0.0, lo - u - K_CUSUM)
            path.append(hi if hi >= lo else -lo)
        out = [abs(c) > limit for c in path]
    else:
        raise ValueError(f"unknown chart {kind!r}")
    stat = path[-1]
    since = None
    if out[-1]:
        since = len(out) - 1
        while since > 0 and out[since - 1]:
            since -= 1
    crossed = out[-1] and (len(out) == 1 or not out[-2])
    return Chart("warn" if crossed else "ok", stat, limit, centre, sigma, since)

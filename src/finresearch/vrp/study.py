"""The vault study `vrp_primary`'s statistics (P3.6, #389; evals/experiments/vault/vrp.md and
evals/experiments/vrp_collectable/PREREG.md). Pure: the sessions come from vrp/data.py or a test.

    windows: one per monthly expiry t that is a stored session; RV(t, t + 30 calendar days) over the sessions in
             (t, t + 30 cd]: n x the per-session Yang-Zhang variance of their within-contract front-future bars, x 365/30
             (calendar-day variance, as sigma^2_MF's N365/N30); complete only when the data reach t + 30 cd
    primary: VRP_t = (sigma^2_MF,t - RV) / sigma^2_MF,t, mean with a Newey-West (Bartlett, lag 1) t and 95 % CI,
             share of negative windows; secondary (labelled): mean ln(sigma^2_MF,t / RV)
    terciles: the payoff by the tercile of sigma^2_MF,t among all EARLIER window starts (point in time), after a
             12-window burn-in; descriptive only
    gate:    daily 100 sqrt(sigma^2_MF) vs India VIX: corr of daily changes >= 0.9 AND |median(own - VIX)| < 1 pt
    z pair:  E[z^2] beside E|z| / sqrt(2/pi) (both 1 for a normal z)

Windows that start 28 days apart (February) overlap the next by up to 2 days: Newey-West's lag 1 absorbs that.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from statistics import NormalDist, median

import numpy as np

from finresearch.fincalc import realvol as rv

HORIZON_DAYS = 30  # calendar days
NW_LAG = 1
CI_LEVEL = 0.95
TERCILE_BURN_IN = 12  # windows
GATE_MIN_CORR = 0.9
GATE_MAX_GAP = 1.0  # vol points
GATE_MIN_DAYS = 20  # daily-change pairs below this: the gate is unknown, which fails
LOG_RATIO_LABEL = "positive under a zero premium by about half Var(ln RV)"


@dataclass(frozen=True)
class Day:
    day: date
    bar: rv.Bar | None  # the front future's bar (prev_close: the same contract's previous close)
    mf30: float | None  # sigma^2_MF, 30-day, annualised (calendar days)
    mf30_reason: str | None
    mf30_flat: float | None  # the flat-extension sensitivity (not the primary)
    vix: float | None  # India VIX close, %


@dataclass(frozen=True)
class Window:
    start: date
    mf30: float | None
    rv: float | None
    sessions: int
    payoff: float | None
    log_ratio: float | None
    reason: str | None


def windows(days: Sequence[Day], starts: set[date], data_end: date) -> list[Window]:
    """One window per start that is a stored session (sorted), each known or unknown with its reason."""
    out: list[Window] = []
    for i, d in enumerate(days):
        if d.day not in starts:
            continue
        end = d.day + timedelta(HORIZON_DAYS)
        inside = [x for x in days[i + 1 :] if x.day <= end]
        n = len(inside)
        if d.mf30 is None:
            out.append(Window(d.day, None, None, n, None, None, d.mf30_reason or "sigma^2_MF unknown"))
            continue
        if data_end < end:
            out.append(Window(d.day, d.mf30, None, n, None, None, f"the data end {data_end} is before {end}"))
            continue
        bars = [x.bar for x in inside]
        yz = None if any(b is None for b in bars) else rv.yang_zhang(bars)  # type: ignore[arg-type]
        if yz is None:
            out.append(
                Window(d.day, d.mf30, None, n, None, None, "RV unknown (an invalid bar in the window)")
            )
            continue
        real = n * yz * 365 / HORIZON_DAYS
        lr = math.log(d.mf30 / real) if real > 0 else None
        out.append(Window(d.day, d.mf30, real, n, (d.mf30 - real) / d.mf30, lr, None))
    return out


def category(reason: str | None) -> str:
    """A reason with every number and date replaced by '#': counted reasons never carry a day's price, strike or
    date (a vault read writes aggregates only, vault PREREG.md section 2)."""
    return re.sub(r"\d[\d.,\-]*", "#", reason or "unknown")


def nw_mean(x: Sequence[float], lag: int = NW_LAG, level: float = CI_LEVEL) -> dict:
    """Mean, Newey-West (Bartlett) standard error, t and the normal CI; t, se and CI unknown below lag + 2 values."""
    n = len(x)
    out: dict = {"n": n, "mean": sum(x) / n if n else None, "se": None, "t": None, "ci95": None}
    if n < lag + 2:
        return out
    m = out["mean"]
    e = [v - m for v in x]
    s = sum(v * v for v in e) / n
    for k in range(1, lag + 1):
        s += 2 * (1 - k / (lag + 1)) * sum(e[i] * e[i - k] for i in range(k, n)) / n
    if s <= 0:
        return out
    se = math.sqrt(s / n)
    z = NormalDist().inv_cdf(0.5 + level / 2)
    out.update(se=se, t=m / se, ci95=(m - z * se, m + z * se))
    return out


def primary(ws: Sequence[Window]) -> dict:
    known = [w for w in ws if w.payoff is not None]
    pay = [w.payoff for w in known]
    logs = [w.log_ratio for w in known if w.log_ratio is not None]
    return {**nw_mean(pay), "unknown": len(ws) - len(known),  # type: ignore[arg-type]
            "unknown_reasons": dict(Counter(category(w.reason) for w in ws if w.payoff is None)),
            "share_negative": sum(1 for p in pay if p < 0) / len(pay) if pay else None,  # type: ignore[operator]
            "log_ratio": {**nw_mean(logs), "label": LOG_RATIO_LABEL}}  # type: ignore[arg-type]  # fmt: skip


def terciles(ws: Sequence[Window], burn_in: int = TERCILE_BURN_IN) -> dict:
    """The payoff by the tercile of sigma^2_MF among all earlier window starts with a known sigma^2_MF."""
    seen = [w for w in sorted(ws, key=lambda w: w.start) if w.mf30 is not None]
    groups: dict[str, list[float]] = {"1": [], "2": [], "3": []}
    for i, w in enumerate(seen):
        if i < burn_in or w.payoff is None:
            continue
        q1, q2 = np.quantile([x.mf30 for x in seen[:i]], [1 / 3, 2 / 3])
        groups["1" if w.mf30 <= q1 else "2" if w.mf30 <= q2 else "3"].append(w.payoff)  # type: ignore[operator]
    return {"burn_in": burn_in, "label": "descriptive only: no threshold, no adoption",
            "by_tercile": {k: nw_mean(v) for k, v in groups.items()}}  # fmt: skip


def gate(days: Sequence[Day]) -> dict:
    """Own 30-day model-free vol against India VIX on the days both are known (one era's days)."""
    pts = [(d.day, 100 * math.sqrt(d.mf30), d.vix) for d in days if d.mf30 is not None and d.vix is not None]
    out: dict = {"days": len(pts), "n": 0, "corr_changes": None, "median_gap": None, "passes": False,
                 "reason": None}  # fmt: skip
    if not pts:
        out["reason"] = "no India VIX close on a day with sigma^2_MF: not VIX-checked"
        return out
    pos = {d.day: i for i, d in enumerate(days)}
    ch = [(b[1] - a[1], b[2] - a[2]) for a, b in pairwise(pts) if pos[b[0]] == pos[a[0]] + 1]
    out["n"] = len(ch)
    out["median_gap"] = median(p[1] - p[2] for p in pts)
    if len(ch) < GATE_MIN_DAYS:
        out["reason"] = f"fewer than {GATE_MIN_DAYS} consecutive-session changes: unknown, which fails"
        return out
    a, b = np.array([c[0] for c in ch]), np.array([c[1] for c in ch])
    if a.std() == 0 or b.std() == 0:
        out["reason"] = "no variation in the daily changes: unknown, which fails"
        return out
    out["corr_changes"] = float(np.corrcoef(a, b)[0, 1])
    out["passes"] = out["corr_changes"] >= GATE_MIN_CORR and abs(out["median_gap"]) < GATE_MAX_GAP
    return out


def z_stats(zs: Sequence[float | None]) -> dict:
    """E[z^2] and E|z| / sqrt(2/pi) of IV-scaled terminal moves (both 1 for a standard normal z)."""
    k = [z for z in zs if z is not None]
    n = len(k)
    return {"n": n, "unknown": len(zs) - n, "e_z2": sum(z * z for z in k) / n if n else None,
            "e_abs_z_scaled": sum(abs(z) for z in k) / n / math.sqrt(2 / math.pi) if n else None}  # fmt: skip

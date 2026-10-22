"""Beta of each F&O underlying's front-future returns to NIFTY's, as registered in `beta_specs` (#394, #395;
evals/experiments/book_betas/PREREG.md). Pure: the caller passes (d0, d1, log return) series built by
`returns_with_prev` from stored settlement prices.

EWMA-weighted least squares with an intercept on NIFTY's session calendar, with its standard error and the
conservative band (lo = β − z·SE, hi = max(floor, β + z·SE)) that every P&L under a NIFTY move uses. Too little
history, gaps, a gap at the end of the series or a stale series leave β unknown with the reason: never 0, never 1.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from finresearch.fincalc import beta_prereg
from finresearch.fincalc import beta_specs as specs
from finresearch.fincalc.fno_history import front_future_returns


class PreregistrationError(RuntimeError):
    pass


def require_registered() -> None:
    if specs.registration_hash() != beta_prereg.HASH:
        raise PreregistrationError(
            "fincalc/beta_specs.py differs from its registration (beta_prereg.HASH): no beta is estimated until the "
            "change is recorded as post hoc with a new version and hash"
        )


@dataclass(frozen=True)
class BetaEstimate:
    """One underlying's beta to NIFTY: the point estimate, its SE and band, how many paired returns and missing NIFTY
    sessions the window had, the window's first and last session (`as_of`) and the half-life; or `reason` (all
    figures None) when unknown."""

    underlying: str
    beta: float | None
    se: float | None
    lo: float | None
    hi: float | None
    n: int = 0
    missing: int = 0
    n_eff: float | None = None
    window_start: date | None = None
    as_of: date | None = None
    half_life: int = specs.SPEC.half_life_sessions
    reason: str | None = None

    @property
    def known(self) -> bool:
        return self.reason is None and self.lo is not None and self.hi is not None


NIFTY = BetaEstimate(
    "NIFTY", 1.0, None, 1.0, 1.0, half_life=0
)  # the market itself: 1 by definition, no estimate


def returns_with_prev(futs: Sequence[tuple[date, date, float]],
                      lots: Mapping[tuple[date, date], int] | None = None) -> list[tuple[date, date, float]]:  # fmt: skip
    """`fno_history.front_future_returns` (the registered roll rule) with each return's previous stored session d0:
    (d0, d1, r). A return the roll rule leaves out (no common contract, a quarantined move) is simply absent."""
    days = sorted({d for d, _, p in futs if p and p > 0})
    prev = dict((d1, d0) for d0, d1 in pairwise(days))
    return [(prev[d1], d1, r) for d1, r in front_future_returns(futs, lots=lots)]


def unknown(u: str, why: str, **kw: object) -> BetaEstimate:
    return BetaEstimate(
        u.upper(), None, None, None, None, reason=f"{u.upper()}: beta to NIFTY unknown, {why}", **kw
    )  # type: ignore[arg-type]


def estimate(underlying: str, nifty: Sequence[tuple[date, date, float]], other: Sequence[tuple[date, date, float]],
             today: date, *, spec: specs.BetaSpec = specs.SPEC) -> BetaEstimate:  # fmt: skip
    """The registered estimate of `underlying`'s beta from both (d0, d1, r) series, as of NIFTY's last return."""
    if spec == specs.SPEC:
        require_registered()
    cal = sorted({(d0, d1) for d0, d1, _ in nifty})[-spec.window_sessions :]
    if not cal:
        return unknown(underlying, "NIFTY's front-future series is empty")
    xs = {(d0, d1): r for d0, d1, r in nifty}
    ys = {(d0, d1): r for d0, d1, r in other}
    as_of, start = cal[-1][1], cal[0][1]
    kw = {"window_start": start, "as_of": as_of, "half_life": spec.half_life_sessions}
    lam = 2 ** (-1 / spec.half_life_sessions)
    k_last = len(cal) - 1
    pts = [(lam ** (k_last - i), xs[s], ys[s]) for i, s in enumerate(cal) if s in ys]
    missing = len(cal) - len(pts)
    kw |= {"n": len(pts), "missing": missing}
    if (today - as_of).days > spec.max_age_days:
        return unknown(underlying, f"the futures series is stale (last session {as_of:%d-%b-%Y})", **kw)
    if spec.recent_sessions and any(s not in ys for s in cal[-spec.recent_sessions :]):
        return unknown(underlying, f"a return is missing in the last {spec.recent_sessions} sessions (a roll or "
                                    "data gap)", **kw)  # fmt: skip
    if len(pts) < spec.min_returns:
        return unknown(underlying, f"only {len(pts)} paired returns (needs {spec.min_returns})", **kw)
    if missing > spec.max_missing:
        return unknown(underlying, f"{missing} sessions without a return in the window (at most "
                                    f"{spec.max_missing})", **kw)  # fmt: skip
    sw = sum(w for w, _, _ in pts)
    xb, yb = sum(w * x for w, x, _ in pts) / sw, sum(w * y for w, _, y in pts) / sw
    sxx = sum(w * (x - xb) ** 2 for w, x, _ in pts)
    n_eff = sw * sw / sum(w * w for w, _, _ in pts)
    if sxx <= 0 or n_eff <= 2:
        return unknown(underlying, "NIFTY's returns in the window do not vary enough", **kw)
    beta = sum(w * (x - xb) * (y - yb) for w, x, y in pts) / sxx
    s2 = sum(w * (y - yb - beta * (x - xb)) ** 2 for w, x, y in pts) / sw * n_eff / (n_eff - 2)
    se = math.sqrt(s2 / (n_eff * sxx / sw))
    return BetaEstimate(underlying.upper(), beta, se, beta - spec.z * se, max(spec.floor, beta + spec.z * se),
                        n_eff=n_eff, **kw)  # type: ignore[arg-type]  # fmt: skip


def as_json(e: BetaEstimate) -> dict[str, object]:
    """What the scenario page and the plan card show for one estimate (the registered z and floor with it)."""
    return {"underlying": e.underlying, "beta": _r(e.beta), "se": _r(e.se), "lo": _r(e.lo), "hi": _r(e.hi),
            "n": e.n, "missing": e.missing, "window_start": e.window_start.isoformat() if e.window_start else None,
            "as_of": e.as_of.isoformat() if e.as_of else None, "half_life": e.half_life, "z": specs.SPEC.z,
            "floor": specs.SPEC.floor, "reason": e.reason}  # fmt: skip


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)

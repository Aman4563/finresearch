"""The registration of the beta-to-NIFTY estimator for the F&O book's scenarios and the common-shock sizing term
(#394, #395): data, roll rule, returns, weights, the minimum history, the gap and staleness rules, the standard error
and the conservative band, hashed by `registration_hash()` and pinned in `beta_prereg.py` before the estimator read
any stored futures price (evals/experiments/book_betas/PREREG.md).

Never edit a registered value in place: a change is a new SPEC_VERSION recorded as post hoc in the experiment's
ADDENDUM.md (never PREREG.md), with its new hash committed before the changed estimator reads real data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

SPEC_VERSION = "book-beta-v1"


@dataclass(frozen=True)
class BetaSpec:
    source: str = ("fno_bhav settlement prices of futures (instrument IDF for indices, STF for stocks); NIFTY's own "
                   "front future is the market series (not the index close)")  # fmt: skip
    roll: str = ("fincalc.fno_history.front_future_returns with the stored lot sizes: the return for session d1 is "
                 "taken within one contract, the earliest expiry after d0 settled on both d0 and d1, so the front-"
                 "month change never makes a return across two contracts; a lot change is adjusted by its factor "
                 "when that explains the move; any |ln| > 0.25 still is quarantined (left out, counted as missing)")  # fmt: skip
    returns: str = "log returns ln(F_d1 / F_d0) of settlement prices"
    calendar: str = ("NIFTY's sessions: the days with a NIFTY front-future return; an underlying's return for d1 is "
                     "paired only when its d0 is the same previous NIFTY session (a two-session return is missing)")  # fmt: skip
    window_sessions: int = 250  # the last 250 NIFTY returns up to as_of
    half_life_sessions: int = 63  # w = 2^(-k / 63), k = NIFTY sessions back from as_of (k = 0 at as_of)
    regression: str = ("weighted least squares with an intercept: xbar = Σwx/Σw, ybar = Σwy/Σw, "
                       "Sxx = Σw(x - xbar)², beta = Σw(x - xbar)(y - ybar) / Sxx, e = y - ybar - beta (x - xbar)")  # fmt: skip
    standard_error: str = ("n_eff = (Σw)² / Σw²; s² = (Σw e² / Σw) x n_eff / (n_eff - 2); "
                           "SE = sqrt(s² / (n_eff x Sxx / Σw)) (ordinary OLS SE when every w = 1)")  # fmt: skip
    min_returns: int = 120  # paired returns in the window
    max_missing: int = 25  # NIFTY sessions in the window without a paired return (10 % of the window)
    recent_sessions: int = 5  # a missing pair among the last 5 NIFTY sessions (a roll or data gap) = unknown
    max_age_days: int = 7  # as_of (the last NIFTY return) at most 7 calendar days before today (IST)
    z: float = 1.645  # one-sided 95 %
    floor: float = 1.0  # the band's upper end is never below 1: no underlying is assumed to fall less than NIFTY
    band: str = ("lo = beta - z SE (not floored), hi = max(floor, beta + z SE); NIFTY = (1, 1) with no estimate. "
                 "A P&L under a NIFTY move m is evaluated, per underlying, at both ends (each underlying moves by "
                 "its end x m) and the worse of the two is taken, then summed over underlyings: the worse of the "
                 "band's two ends, not the worst over the interval")  # fmt: skip
    point_use: str = "the point estimate is shown and used only for the NIFTY-equivalent delta (a display, no gate)"
    unknown: str = ("fewer than min_returns pairs, more than max_missing missing, a missing pair in the last "
                    "recent_sessions, a stale as_of, or Sxx = 0 or n_eff <= 2: beta unknown with the reason "
                    "(never 0, never 1)")  # fmt: skip


SPEC = BetaSpec()


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def registration() -> dict:
    return {"version": SPEC_VERSION, "spec": asdict(SPEC)}


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()

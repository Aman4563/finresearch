"""The registration of the volatility forecast race (P3.7, #390): target, models, refits, losses, tests, windows and
the adoption gate, hashed by `registration_hash()` and pinned in `prereg.py` before any code reads a stored session.

It implements the vault study `vol_forecast_race` (evals/experiments/vault/vol_race.md, sealed in the vault pack) and
fixes what that file leaves open. Every numeric setting is read from the module that uses it, so editing one moves
the hash. Never edit a registered value in place: a change is a new SPEC_VERSION recorded as post hoc in
evals/experiments/vol_forecast_race/ADDENDUM.md (never PREREG.md), with its hash committed before the changed race
reads a stored session. The race searches no strategies: VARIANTS_TRIED (backtest/specs.py) is untouched.
"""

from __future__ import annotations

import hashlib
import json

from finresearch.fincalc import fno_history as fh
from finresearch.fincalc import realvol as rv
from finresearch.fincalc import taildist as td
from finresearch.volrace import race

SPEC_VERSION = "vol-forecast-race-v1"
VAULT_STUDY = "vol_forecast_race"
VAULT_FILE_SHA256 = (
    "8434113de0316c730e117977ce1436ab32a4d9f04aa4b7139fd922c1b682f01f"  # vol_race.md at registration
)
UNDERLYINGS = {"primary": "NIFTY", "secondary": ("BANKNIFTY",)}
DATA = {
    "bars": "fno_bhav index futures (option_type XX) of the underlying: each session's front contract is its nearest "
            "expiry on or after the session; its open, high, low and close, with prev_close = the SAME contract's "
            "close on the immediately previous stored session (none: the bar's overnight term is unknown)",
    "invalid_bar": "a missing or non-positive price, high < max(open, close) or low > min(open, close): every "
                   "estimate over a window holding it is unknown and counted, never dropped silently",
    "implied": "each stored session's per-expiry ATM Black-76 IVs, fincalc.fno_history.day_summary on the stored "
               "option settles (the fno_iv_eod engine: market settles only, ATM within the band, forward from the "
               "same-expiry future, else parity, else spot); sigma^2_MF (P3.6) does not exist yet",
    "w_h": "total implied variance to the calendar date of session t + h, days / 365: linear in total variance "
           "between the bracketing expiries, flat at the shortest expiry's IV before it, unknown beyond the longest",
    "iv2": "ln IV^2_t of HAR-IV = ln(w_h / tau_h), tau_h = calendar days to session t + h / 365 (horizon-matched)",
    "clock": "calendar days / 365 (P3.1's business time is not adopted); the NSE session calendar is published in "
             "advance, so the date of session t + h is known at the origin",
    "returns": "close-to-close within-contract log returns ln(C / prev_close) for EWMA and GARCH; an unknown return "
               "is left out of their input and counted",
}  # fmt: skip
TARGET = "y_t,h = h x Yang-Zhang per-session variance over sessions t+1..t+h (k at n = h): total variance"
DAILY = "d_t = (ln O_t / prev close)^2 + Rogers-Satchell_t: the HAR regressors' daily variance"
FIT = {
    "regression": "OLS of ln y on the model's regressors, forecast exp(x'b + s^2 / 2) with s^2 = SSR / (n - p)",
    "refit": "the first session of each calendar month at or after the burn-in; forecasts from a refit to the next",
    "purge": "training origins s with s + h <= the refit session (expanding window); the latest MAX_ROWS rows",
    "garch": "Gaussian QMLE, zero mean, arch's backcast, on the last GARCH_WINDOW known returns up to the refit; "
             "between refits the fitted filter runs on the returns to the origin",
    "unknown": "a forecast is None when its fit or any input is unknown; nothing becomes 0",
}  # fmt: skip


def settings() -> dict:
    """Every numeric setting, read from the module that uses it at call time (so editing one moves the hash)."""
    return {
        "horizons": race.HORIZONS, "trail": race.TRAIL, "max_rows": race.MAX_ROWS, "garch_window": race.GARCH_WINDOW,
        "dm_alpha": race.DM_ALPHA, "adopt_min_horizons": race.ADOPT_MIN_HORIZONS, "mcs_alpha": race.MCS_ALPHA,
        "mcs_reps": race.MCS_REPS, "mcs_mean_block": race.MCS_MEAN_BLOCK, "seed": race.SEED,
        "min_mcs_rows": race.MIN_MCS_ROWS, "yz_alpha": rv.YZ_ALPHA, "har": (rv.HAR_WEEK, rv.HAR_MONTH),
        "garch_start": rv.GARCH_START, "gjr_start": td.GJR_START, "gjr_min_returns": td.GJR_MIN_RETURNS,
        "ewma_lambda": td.EWMA_LAMBDA, "backcast": (td.BACKCAST_TAU, td.BACKCAST_DECAY),
        "nelder_mead": (td.NM_STEP, td.NM_XATOL, td.NM_FATOL, td.NM_MAXFEV, td.NM_RESTARTS),
        "iv_engine": {"rate": fh.RATE, "min_contracts": fh.MIN_CONTRACTS, "atm_band": fh.ATM_BAND},
    }  # fmt: skip


LOSSES = {"primary": "QLIKE = y / f - ln(y / f) - 1", "secondary": "MSE = (y - f)^2"}
TESTS = {
    "grid": "non-overlapping windows: origins t0, t0 + h, ... from the first origin at or after the burn-in where "
            "the target, the challenger and the benchmark are known; grid origins missing one of them are skipped",
    "dm": "Diebold-Mariano with the HLN correction (direction.metrics.diebold_mariano_hln, h = 1 on the "
          "non-overlapping grid), one-sided (challenger better), benchmark loss minus challenger loss on QLIKE, n >= 10",
    "holm": "Holm across the horizons, per window",
    "mcs": "Hansen, Lunde and Nason T_max MCS at 90 % over every model, on QLIKE, on the grid origins where every "
           "model is known (the rest counted), stationary bootstrap",
    "mz": "Mincer-Zarnowitz y = a + b f per model, reported only",
}  # fmt: skip
WINDOWS = {
    "vault": {"start": "2008-01-01", "end": "2021-12-31", "burn_in": 500,
              "access": "one logged read through vault.guard.vault_read('vol_forecast_race', ...) after vrp_primary"},
    "recent": {"start": "2022-01-01", "end": "2026-12-31", "burn_in": 250,
               "access": "the app database; labelled seen (2022-26 was read by earlier studies, not for this race)"},
}  # fmt: skip
ADOPTION = ("The primary challenger (log HAR-IV on Yang-Zhang RV of the NIFTY front future, no event dummies) is "
            "adopted only if, in BOTH the vault and the recent window, it beats the benchmark on QLIKE at "
            "Holm-adjusted DM p < 0.05 AND is in the 90 % MCS at >= 2 of the 3 horizons, and at least two of those "
            "horizons are the same in both windows. Every other model is secondary and cannot be adopted. BANKNIFTY is reported only. "
            "Forecast skill alone never unlocks ENTER and changes no card.")  # fmt: skip


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def registration() -> dict:
    return {"version": SPEC_VERSION, "vault_study": VAULT_STUDY, "vault_file_sha256": VAULT_FILE_SHA256,
            "underlyings": UNDERLYINGS, "data": DATA, "target": TARGET, "daily": DAILY, "models": race.MODELS,
            "challenger": race.CHALLENGER, "benchmark": race.BENCHMARK, "fit": FIT, "settings": settings(),
            "losses": LOSSES, "tests": TESTS, "windows": WINDOWS, "adoption": ADOPTION}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()

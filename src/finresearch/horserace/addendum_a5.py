"""Post-hoc addendum A5 to the card horse race (#377): the tail-aware physical challengers of P3.4 (#387). Written and
hashed before any replay code computed them on a stored session; its hash is pinned in prereg.ADDENDUM_A5_HASH and the
replay refuses to run when it differs. The registered `card-horse-race-v1` (horserace/specs.py) and addenda A1-A3 are
unchanged; A4 is registered in parallel by another slice and A5 chains from A3 (the challenger lists are unioned when
both are merged). Prose: evals/experiments/card_horse_race/ADDENDUM.md. A change is a new ADDENDUM_VERSION, never an
edit. Every fitted setting (fincalc/taildist.py's registered numbers and the bucket, seed and refit rules here) is
hashed with it.
"""

from __future__ import annotations

import hashlib

from finresearch.fincalc import taildist as td
from finresearch.horserace import addendum, addendum_a2, addendum_a3, specs

ADDENDUM_VERSION = "card-horse-race-v1-addendum-A5"
BASE_VERSION = specs.SPEC_VERSION
EMPIRICAL, FHS = "p_rw_emp", "p_rw_fhs"
CHALLENGERS = (*addendum_a3.CHALLENGERS, EMPIRICAL, FHS)
# tenor buckets by calendar days to expiry: (lowest DTE, highest DTE, expiries admitted to the z sample)
BUCKETS = {"short": (1, 14, "any"), "long": (15, 45, "monthly")}
SEED = 387  # each card's generator: sha256 of "SEED|underlying|spec|origin|expiry" (independent of run order)
GJR_REFIT = "the first stored session of each calendar month"
GPD_TAIL = None  # off: the 95th percentile of |z| leaves about 5-10 exceedances per bucket in 2022-26 (A5.8)
ITEMS = {
    "A5.1 z sample": "per underlying and bucket (BUCKETS), one z per expiry: z = [ln(S_T / F) + sigma^2 T / 2] / "
                     "(sigma sqrt(T)) at the expiry's entry session = the first stored session whose DTE is inside "
                     "the bucket (long bucket: a monthly expiry, Day.is_monthly) with a known A1.5 forward F and A1.8 "
                     "ATM IV sigma; T = DTE / 365; S_T = backtest.pricing.settlement_price on the expiry's last "
                     "session (unknown: no z). Expanding window from the first stored session (warm-up included)",
    "A5.2 no look-ahead": "a card at origin d uses only z whose settlement session is strictly before d, and only "
                          "returns up to and including d (Clocked); tested",
    "A5.3 p_rw_emp (M2)": "taildist.empirical_forecast in the card's bucket (by its DTE; outside every bucket: "
                          "unknown) with sigma = the card's A1.8 ATM IV (p_rn_atm's), F = the A1.5 forward, A1.6's t, "
                          "rate, costs, legs and fills; kappa = mean z^2 of the sample; the point is exact on the "
                          "empirical z distribution; fewer than MIN_Z z or no ATM IV: unknown. GPD tail off (A5.8)",
    "A5.4 p_rw_fhs (M3)": "taildist.fhs_forecast with a GJR-GARCH(1,1) Gaussian QMLE (taildist.gjr_fit: last "
                          "GJR_WINDOW A1.7 same-instrument returns, at least GJR_MIN_RETURNS, Nelder-Mead from "
                          "GJR_START with NM_* settings) refitted on GJR_REFIT using returns up to that session, "
                          "filtered daily through the origin; steps = taildist.weekday_steps(origin, expiry); "
                          "kappa = A5.3's sample kappa (unknown with fewer than MIN_Z z); PATHS paths, FHS_BLOCK-day "
                          "residual blocks, rescaled to kappa sigma_IV^2 T; a failed or short fit: unknown",
    "A5.5 forecasts": "PoP, EV and the 20 dots (A1.9's quantile rule) from taildist.sample_outcome; the exit cost "
                      "is A1.6's exercise STT on in-the-money longs. The EV bootstrap interval, ES97.5 and kappa "
                      "are logged per card, not scored; the replay computes no EV interval (boot = 0)",
    "A5.6 race": "race.run with challengers = addendum_a3.CHALLENGERS + (p_rw_emp, p_rw_fhs); incumbent p_rw, "
                 "scores, tests and the gate unchanged; the registered Holm family grows by both",
    "A5.7 windows": "replay windows design, seen and tuesday only, none of which can adopt; the live card's PoP, EV "
                    "and gates are unchanged, so adoption needs forward shadow logging or the vault, each "
                    "registered separately first",
    "A5.8 off or later": "the GPD tail (too few exceedances), the EWMA-filtered FHS, the robust EV gate's lambda and "
                         "ES budget (taildist.robust_gate stays informational until a model is validated), the M3 "
                         "EV interval and the drawdown bands are not registered here; each is a later addendum",
}  # fmt: skip


def fitted_settings() -> dict:
    return {"ES_ALPHA": td.ES_ALPHA, "MIN_Z": td.MIN_Z, "BLOCK_MEAN": td.BLOCK_MEAN, "EV_BOOT": td.EV_BOOT,
            "EV_LEVEL": td.EV_LEVEL, "PATHS": td.PATHS, "FHS_BLOCK": td.FHS_BLOCK, "GJR_WINDOW": td.GJR_WINDOW,
            "GJR_MIN_RETURNS": td.GJR_MIN_RETURNS, "GJR_START": list(td.GJR_START), "NM_STEP": td.NM_STEP,
            "NM_XATOL": td.NM_XATOL, "NM_FATOL": td.NM_FATOL, "NM_MAXFEV": td.NM_MAXFEV,
            "NM_RESTARTS": td.NM_RESTARTS, "BACKCAST": [td.BACKCAST_TAU, td.BACKCAST_DECAY],
            "EWMA_LAMBDA": td.EWMA_LAMBDA, "GPD_THETA_GRID": td.GPD_THETA_GRID, "DD_BLOCK": td.DD_BLOCK,
            "DD_SIMS": td.DD_SIMS, "BUCKETS": BUCKETS, "SEED": SEED, "GJR_REFIT": GJR_REFIT, "GPD_TAIL": GPD_TAIL}  # fmt: skip


def registration() -> dict:
    return {
        "version": ADDENDUM_VERSION,
        "base": BASE_VERSION,
        "base_hash": specs.registration_hash(),
        "a1_hash": addendum.addendum_hash(),
        "a2_hash": addendum_a2.addendum_hash(),
        "a3_hash": addendum_a3.addendum_hash(),
        "challengers": CHALLENGERS,
        "items": ITEMS,
        "settings": fitted_settings(),
    }


def addendum_hash() -> str:
    return hashlib.sha256(specs.canonical(registration()).encode()).hexdigest()

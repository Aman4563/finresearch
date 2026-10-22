"""The registration of the collectable VRP study (P3.6, #389): measures, windows, structures, costs, statistics and
the reading rule, hashed by `registration_hash()` and pinned in `prereg.py` before any code that reads a stored
session exists.

It implements the vault study `vrp_primary` (evals/experiments/vault/vrp.md, sealed in the vault pack #364) without
editing it and fixes what that file leaves open. Every numeric setting is read from the module that uses it, so
editing one moves the hash. Never edit a registered value in place: a change is a new SPEC_VERSION recorded as post
hoc in evals/experiments/vrp_collectable/ADDENDUM.md (never PREREG.md), with its hash committed before the changed
study reads a stored session. Nothing here searches strategies: VARIANTS_TRIED (backtest/specs.py) is untouched.
"""

from __future__ import annotations

import hashlib
import json

from finresearch.backtest.criteria_v2 import CRITERIA_V2
from finresearch.backtest.specs import BOOK
from finresearch.fincalc import fno_history as fh
from finresearch.fincalc import margin_approx, mfiv
from finresearch.fincalc import realvol as rv
from finresearch.vrp import collect, measure, run, study

SPEC_VERSION = "vrp-collectable-v1"
VAULT_STUDY = "vrp_primary"
VAULT_FILE_SHA256 = (
    "a9aa38e8e676cdf4ba14ddfe13916eb0dbb88696781dc1f3640d174427719be5"  # vrp.md at registration
)
UNDERLYINGS = {"primary": "NIFTY", "secondary": ("BANKNIFTY",)}
DATA = {
    "options": "fno_bhav index options of the underlying; a price only where the settle is a market price "
               "(fno_history.market_settle: >= MIN_CONTRACTS traded and settle == close within 0.5 % + 5 paise); "
               "every listed strike is kept so the two-consecutive-misses stop rule sees it",
    "forward": "the same-expiry future's settle (India VIX), else Cboe's parity forward",
    "bars": "the front future (nearest expiry on or after the session), within-contract OHLC with prev_close = the "
            "SAME contract's close on the previous stored session (as volrace/data.py)",
    "vix": "index_eod symbol 'INDIA VIX' close (P1.4); days without it are not VIX-checked",
    "final": "backtest.pricing.settlement_price (#352) on the contract's last stored session (a holiday can move it "
             "earlier): the uniform expiring-option settle checked within 1 bp of the expiring future or underlying, "
             "else the expiring future, else the underlying; none: unknown",
    "lots": "backtest.dbhistory.DbHistory.lot (the backtest's rule: NSE's dated circulars on vault days, later the "
            "row's or fno_contract's lot); none: unknown",
    "monthly": "the latest listed expiry of each calendar month (vrp/measure.monthly_expiries)",
}  # fmt: skip
MEASURE = {
    "sigma2_mf": "fincalc.mfiv.term_variance per term (Cboe v6.0 section 3 / India VIX white paper 3-5, verified on "
                 "both published examples), 30-day by mfiv.thirty_day in minutes (extrapolating, with a negative "
                 "far weight, when both monthly terms are beyond 30 days, as at a monthly expiry's close)",
    "terms": "near and next monthly; rolled to next and far with ROLL_SESSIONS or fewer sessions left",
    "common_range": "the registered primary uses band = the common moneyness range |ln K/F| fixed by vault_data_audit "
                    "check 8 (covered on >= 90 % of days in every era) and passed to the run; the vault read is "
                    "refused without it; a recent-window run without it is labelled 'full traded strip, not "
                    "era-comparable'",
    "flat": "sensitivity: the strip extended beyond its ends at the edge strikes' own Black-76 IV (#389's single-day "
            "figure)",
    "rv": "RV(t, t + 30 cd) = n x Yang-Zhang per-session variance of the n sessions in (t, t + 30 cd], x 365/30",
    "z": "near monthly expiry at each window start: z = ln(final / F_t) / sqrt(v T), v = the near term's "
         "sigma^2_MF ('mf') or its ATM IV^2 (fno_history.expiry_iv, 'atm')",
}  # fmt: skip
STRUCTURES = {
    "straddle": "short ATM straddle, 1 lot: the strike nearest the same-expiry future with both a call and a put "
                "market settle at the entry settle (ties: the lower), from each monthly expiry's close to the next "
                "monthly expiry, cash-settled at the final settlement price",
    "put25": "short put at the strike whose Black-76 forward delta is nearest -0.25 (collect.delta_put_strike), "
             "un-hedged, same dates",
    "hedges": "none | band (whole futures lots, re-set at a settle only when |net delta| >= BAND lot) | fractional "
              "(benchmark, labelled not collectable)",
}  # fmt: skip
BANKNIFTY_RULE = ("BANKNIFTY enters only from the first year with >= 10 valid OTM strikes per side on >= 90 % of days "
                  "(vault_data_audit check 8), passed to the run; the vault read refuses BANKNIFTY without it")  # fmt: skip
READING = ("Outcome reported whatever it is; nothing is adopted. Gate (0) fails -> (1) and (2) are labelled 'measure "
           "unvalidated'. (1) mean payoff, NW t (lag 1), 95 % CI, share negative per period; the log ratio labelled. "
           "(2) mean net after costs with the expiry-clustered BCa CI (5,000 draws, seed 389), share negative, "
           "crash-period losses, the margin view, at dated and 2026 rates. One conditional statistic on the vault: "
           "the expanding-tercile split. A VRP-conditioned seller variant needs the after-cost P&L of the form it "
           "would trade positive in BOTH periods (vrp.md); a positive log ratio, vol gap or share of IV > RV never "
           "qualifies. Vault output is aggregates only.")  # fmt: skip


def settings() -> dict:
    """Every numeric setting, read from the module that uses it at call time (so editing one moves the hash)."""
    return {
        "mfiv": {"min_per_side": mfiv.MIN_PER_SIDE, "max_gap": mfiv.MAX_GAP, "n30": mfiv.N30, "n365": mfiv.N365,
                 "minutes_day": mfiv.MINUTES_DAY, "ext": (mfiv.EXT_NEGLIGIBLE, mfiv.EXT_MAX_STRIKES)},
        "roll_sessions": measure.ROLL_SESSIONS,
        "fno": {"rate": fh.RATE, "min_contracts": fh.MIN_CONTRACTS, "atm_band": fh.ATM_BAND},
        "study": {"horizon_days": study.HORIZON_DAYS, "nw_lag": study.NW_LAG, "ci": study.CI_LEVEL,
                  "tercile_burn_in": study.TERCILE_BURN_IN, "gate": (study.GATE_MIN_CORR, study.GATE_MAX_GAP,
                                                                     study.GATE_MIN_DAYS)},
        "yz_alpha": rv.YZ_ALPHA,
        "collect": {"band": collect.BAND, "fut_slippage_bps": collect.FUT_SLIPPAGE_BPS, "put_delta": collect.PUT_DELTA,
                    "today": collect.COLLECTABLE_TODAY, "bootstrap": (collect.BOOTSTRAP_DRAWS, collect.BOOTSTRAP_SEED),
                    "crashes": collect.CRASHES, "hedges": collect.HEDGES},
        "book": {"slippage_tiers": BOOK.slippage_tiers, "min_slippage": BOOK.min_slippage,
                 "slippage_mult": BOOK.slippage_mult, "brokerage_per_order": BOOK.brokerage_per_order},
        "carry": {"cash_share": CRITERIA_V2.cash_share, "collateral_rate": CRITERIA_V2.collateral_rate},
        "margin": margin_approx.PARAMS["index"],
        "periods": run.PERIODS, "eras": run.ERAS,
    }  # fmt: skip


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def registration() -> dict:
    return {"version": SPEC_VERSION, "vault_study": VAULT_STUDY, "vault_file_sha256": VAULT_FILE_SHA256,
            "underlyings": UNDERLYINGS, "data": DATA, "measure": MEASURE, "structures": STRUCTURES,
            "banknifty": BANKNIFTY_RULE, "settings": settings(), "reading": READING,
            "labels": {"log_ratio": study.LOG_RATIO_LABEL, "fractional": run.FRACTIONAL_LABEL,
                       "verdict": run.VERDICT}}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()

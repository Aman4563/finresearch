"""The registration of the stock-option VRP study and of the panel it needs (P1.12, #374): question, panel
definition, sample, statistic, tests and decision rule, hashed by `registration_hash()` and pinned in `prereg.py`
before any panel code existed (evals/experiments/stock_option_vrp/PREREG.md restates it in prose; this file wins).

Every numeric setting is read at call time from the module that uses it (optpanel.params, fincalc.fno_history,
backtest.specs.BOOK), so editing one moves the hash. Never edit a registered value in place: a change is a new
SPEC_VERSION recorded as post hoc in evals/experiments/stock_option_vrp/ADDENDUM.md (never PREREG.md), with its hash
committed before the changed panel or study reads a stored day. The study searches no library strategies, reads no
vault data (stock options are not in the 2008-2021 vault) and leaves backtest/specs.VARIANTS_TRIED unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from finresearch.optpanel import params as p

SPEC_VERSION = "stock-option-vrp-v1"
QUESTION = ("Across NSE stock-option underlyings, does the variance risk premium proxy IV30 - RV20 at the start of a "
            "monthly cycle predict the net-of-cost return of selling that stock's ATM straddle to expiry? This is "
            "the example statistic of issue #374 (spread = IV30 - RV20, decile sorts, next-month ATM straddle "
            "return, month-clustered t, P1.1 costs), registered here as the study that justifies building the panel.")  # fmt: skip
PANEL = {
    "source": "re-parse of the kept raw F&O bhavcopy zips (data/fno_bhav/<year>/, fnohist.store.read_raw): every "
              "STO row and the IDO rows of params.INDEX_UNDERLYINGS; no new download for a stored day; days in the "
              "2008-2021 vault are refused",
    "universe": "every underlying with stock options in the day's file, chosen from the file alone: the held or "
                "watched list (disclosures.store.tracked) is never read, so the panel reveals no portfolio",
    "iv": "fincalc.fno_history.day_summary on the day's market settles (settle == close, contracts >= "
          "MIN_CONTRACTS): near and next ATM Black-76 IV (first expiries >= MIN_DTE days), IV30 by total-variance "
          "interpolation (TARGET_DTE, FLAT_WITHIN) and the near expiry's 25-delta skew (put IV - call IV)",
    "rv20": "fincalc.fno_history.realised_vol_pct over front_future_returns of the stored stock futures (fno_bhav "
            "STF, kept for every stock): within-contract returns, lot-change factor applied and |ln| > "
            "QUARANTINE_LN left out (#369, roadmap P1.7's F&O adjustment)",
    "straddle": "the near expiry's ATM strike (day_summary's): call settle + put settle, with each leg's contracts",
    "buckets": "open interest (units) and contracts traded per ln(K/F) bucket (params.BUCKET_EDGES), calls and "
               "puts apart, all expiries on or after the day, F = each expiry's day_summary forward",
    "liquidity": "option contracts traded with |K/F - 1| <= params.LIQ_BAND, all expiries",
    "rows": "full contract rows kept only for |ln(K/F)| <= params.ROWS_LN_BAND and the params.ROWS_EXPIRIES "
            "nearest expiries on or after the day; contracts with no OI and no trade are dropped (as fno_bhav)",
    "unknown": "a value that cannot be fitted is NULL with the reason counted, never 0",
}  # fmt: skip
BAN = {
    "source": "NSE Clearing's combined open interest file (combineoi): per stock its MWPL and the combined OI at the "
              "end of day D; utilisation u_D = OI_D / MWPL_D",
    "rule": "hysteresis, per stock, in session order: banned for D+1 when u_D > BAN_ENTER, or when banned for D and "
            "u_D >= BAN_EXIT; the state is unknown until the first day that decides it (u > BAN_ENTER or "
            "u < BAN_EXIT) and after any missing file",
    "validation": "the derived list for trade day D+1 against NSE's dated ban notice (fo_secban_DDMMYYYY.csv) on 20 "
                  "sample days drawn with a fixed seed",
}  # fmt: skip
DESIGN = {
    "cycle": "the stock monthly expiry E_m (the expiry of the stock futures); formation day t_m = the first stored "
             "session after E_(m-1); hold to E_m",
    "signal": "s_i = IV30_i - RV20_i (vol points) at t_m from the panel",
    "eligible": "IV30, RV20 and both straddle legs known at t_m for expiry E_m with each leg's contracts >= "
                "MIN_CONTRACTS; a fitted ATM IV on >= MIN_COVERAGE of the last COVERAGE_SESSIONS stored sessions; "
                "the derived ban state for t_m is 'not banned' (banned OR unknown: excluded and counted)",
    "return": "short ATM straddle per rupee of premium: r_i = (C + P - |F_T - K| - costs) / (C + P), entry at t_m "
              "settles, F_T = the E_m stock future's settle on E_m (the final settlement price)",
    "costs": "P1.1's cost model as it stands (backtest.costs): the registered slippage tiers of backtest.specs.BOOK "
             "per leg at entry (widest tier for unknown volume) and fincalc.charges for one lot on t_m's date, plus "
             "exercise STT on the in-the-money leg at expiry; no stock-option quotes are recorded, so a measured "
             "cost model enters only as a new registered variant",
    "statistic": "per cycle S_m = mean r over the top quantile of s minus mean r over the bottom quantile; cycles "
                 "with fewer than MIN_CROSS_SECTION eligible stocks are skipped and counted",
    "test": "one-sided t of mean(S_m) > 0 over cycles (one observation per month: month-clustered), Holm across "
            "params.QUANTILES (deciles primary, quintiles secondary) at ALPHA",
}  # fmt: skip
SAMPLE = {
    "in_sample": "cycles with t_m >= SAMPLE_START and E_m <= SAMPLE_END (about 57 cycles)",
    "seen": "index options and the few held or watched stocks' options were read before; the cross-section of the "
            "other ~200 stocks' options has never been parsed into any table",
    "forward": "the same test, read once, on cycles formed after SAMPLE_END once FORWARD_MIN_CYCLES exist",
}  # fmt: skip
DECISION = ("Evidence of a cross-sectional stock VRP premium only if ALL hold: the decile test's Holm-adjusted "
            "one-sided p < ALPHA; mean S_m > 0 in both halves (t_m on or before SPLIT, and after); mean S_m > 0 "
            "with slippage x STRESS_SLIPPAGE_MULT. Its only consequence is permission to register a trading "
            "variant in its own PREREG; it changes no card, unlocks no ENTER and adds nothing to VARIANTS_TRIED. "
            "Otherwise the result is reported as null.")  # fmt: skip


def settings() -> dict:
    """Every numeric setting, read from the module that uses it at call time (so editing one moves the hash)."""
    from finresearch.backtest import specs as bspecs
    from finresearch.fincalc import fno_history as fh

    return {
        "panel": {"index_underlyings": p.INDEX_UNDERLYINGS, "rows_ln_band": p.ROWS_LN_BAND,
                  "rows_expiries": p.ROWS_EXPIRIES, "liq_band": p.LIQ_BAND, "bucket_edges": p.BUCKET_EDGES},
        "ban": {"enter": p.BAN_ENTER, "exit": p.BAN_EXIT},
        "study": {"sample_start": p.SAMPLE_START, "sample_end": p.SAMPLE_END, "split": p.SPLIT,
                  "coverage_sessions": p.COVERAGE_SESSIONS, "min_coverage": p.MIN_COVERAGE,
                  "min_cross_section": p.MIN_CROSS_SECTION, "quantiles": p.QUANTILES, "alpha": p.ALPHA,
                  "stress_slippage_mult": p.STRESS_SLIPPAGE_MULT, "forward_min_cycles": p.FORWARD_MIN_CYCLES},
        "iv_engine": {"rate": fh.RATE, "min_contracts": fh.MIN_CONTRACTS, "atm_band": fh.ATM_BAND,
                      "min_dte": fh.MIN_DTE, "target_dte": fh.TARGET_DTE, "flat_within": fh.FLAT_WITHIN,
                      "rv_window": fh.RV_WINDOW, "quarantine_ln": fh.QUARANTINE_LN},
        "costs": asdict(bspecs.BOOK),
    }  # fmt: skip


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def registration() -> dict:
    return {"version": SPEC_VERSION, "question": QUESTION, "panel": PANEL, "ban": BAN, "design": DESIGN,
            "sample": SAMPLE, "decision": DECISION, "settings": settings()}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()

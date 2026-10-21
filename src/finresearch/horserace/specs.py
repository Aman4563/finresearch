"""The registration of the card horse race (P2.2, #377): forecasters, scores, windows, sample rules and the adoption
gate, hashed by `registration_hash()` and pinned in `prereg.py` before any score is computed on real outcomes.

Never edit a registered value in place: a change is a new SPEC_VERSION recorded as post hoc in the experiment's
ADDENDUM.md (never PREREG.md), and its new hash is committed before any out-of-sample run
(evals/experiments/card_horse_race/PREREG.md). The harness searches no new strategies: VARIANTS_TRIED of the backtest
(backtest/specs.py) is untouched by anything here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

SPEC_VERSION = "card-horse-race-v1"
UNDERLYINGS = ("NIFTY", "BANKNIFTY")
EVENT_TEXT = ("1{P&L of one set after statutory costs and the engine's tiered slippage > 0 under the structure's "
              "exit plan}; the Rs P&L per set is kept (evals/experiments/card_ledger/PREREG.md for logged cards)")  # fmt: skip

# Forecasters of the binary event. "dots" = it also gives a P&L distribution (20 quantile dots at (i + 0.5) / 20) and
# an EV; only a forecaster with dots can be adopted (it must pass tail coverage).
INCUMBENT = "p_rw"
FORECASTERS = {
    "p_rw": "the card's headline real-world PoP: the less favourable (lower-EV) of p_rw20 and p_rw60 (dots, EV)",
    "p_rw20": "lognormal at the 20-day realised volatility of closes, drift = r (dots, EV; replay only)",
    "p_rw60": "lognormal at the 60-day realised volatility of closes, drift = r (dots, EV; replay only)",
    "p_rn_atm": "lognormal at the ATM IV, drift = r: the option market's own probability (dots, EV; benchmark)",
    "p_base": "trailing base rate: (k + 1) / (n + 2) of earlier resolved events, same underlying and structure",
    "p_gate": "gate-conditional base rate: (k + 1) / (n + 2) of earlier resolved events, same underlying, structure "
              "and gate class (pass = ENTER or NO PROVEN EDGE; fail = WAIT or AVOID)",
}  # fmt: skip
TRAILING_PRIOR = (
    1,
    1,
)  # Beta(1, 1): a trailing rate starts at 1/2 and uses only events resolved before the origin
CHALLENGERS: tuple[
    str, ...
] = ()  # P3 models (IV-scaled empirical, EWMA/GJR-FHS, SVI/Breeden-Litzenberger) are
# added here, each as a new SPEC_VERSION with its hash committed, BEFORE the challenger's out-of-sample score is read


@dataclass(frozen=True)
class Scores:
    primary_binary: str = "brier"  # adoption score for every PoP forecaster
    secondary_binary: tuple[str, ...] = ("log", "murphy", "reliability")
    log_clip: float = 1e-6
    murphy_bins: int = (
        10  # equal-width [0, .1), ..., [.9, 1]; the within-bin residual is reported, never hidden
    )
    distribution: str = "crps"  # secondary, forecasters with dots
    dots: int = 20
    quantile_fn: str = ("piecewise-linear through ((i + 0.5) / 20, dot_i); the end segments extended linearly to "
                        "u = 0 and u = 1")  # fmt: skip
    integration_nodes: int = 2000  # midpoint rule over u for CRPS and ES
    pit_clip: float = 1e-3  # PIT outside the support is clipped to [pit_clip, 1 - pit_clip] and counted
    tail_alpha: float = 0.10  # VaR/ES level read off the dots (q(0.10) lies between dots 2 and 3)
    berkowitz: str = "AR(1) Gaussian, conditional likelihood, LR(3 df): mean 0, variance 1, rho 0"
    z2_sims: int = 10_000  # Acerbi-Szekely Z2 p-value by simulation from each event's quantile function
    seed: int = 377
    ev_regression: str = (
        "PnL = a + b EV, OLS, CR1 expiry-clustered SE, t(G - 1); calibrated iff 1 in CI(b), 0 in CI(a)"
    )
    gate_information: str = "PnL = a + delta 1{gate passes}, OLS, CR1 expiry-clustered SE, two-sided"
    exit_reasons: str = "multinomial log score against the trailing Dirichlet(1) frequencies of exit reasons"
    drawdown_bands: str = (
        "share of realised max drawdowns inside the band against its nominal cover, binomial"
    )


@dataclass(frozen=True)
class Tests:
    dm: str = "Diebold-Mariano, HLN-corrected, one-sided (challenger better), on per-expiry mean score differentials"
    dm_h: int = 1
    dm_lag: int = 1  # Newey-West (Bartlett): consecutive expiries can share a holding window
    dm_min_expiries: int = 10
    holm_family: str = "every non-incumbent forecaster x underlying, per window and score"
    alpha: float = 0.05
    tail_min_events: int = (
        100  # Kupiec, Christoffersen, Acerbi-Szekely, Berkowitz as tests; below it descriptive
    )
    tail_events: str = (
        "one event per (underlying, expiry, structure): the first entry session of the structure"
    )


@dataclass(frozen=True)
class Replay:
    specs: tuple[str, ...] = (
        "nifty_pcs_m",
        "nifty_ccs_m",
        "banknifty_ic_m",
        "nifty_ic_w",
        "nifty_dcs_m",
        "nifty_dps_m",
    )  # the registered index specs (backtest/specs.py), on their own underlying
    origins: str = "every stored session inside the spec's entry window (its DTE range), one card per session"
    legs: str = "the spec's registered delta rule at the origin close; the anchor leg traded >= 10 contracts"
    entry_price: str = "each leg's NSE settlement price on the origin session"
    exits: str = (
        "backtest.engine.manage under the spec's exits, statutory costs and tiered slippage, unchanged"
    )
    forecasts_at: str = (
        "the origin close: only data stored on or before the origin session (backtest Clocked)"
    )
    start: str = "2022-04-01"
    end: str = "the last settled expiry in the stored bhavcopy at the run"


# Windows. The vault (2008-2021) is registered separately (evals/experiments/fno_pop_calibration/PREREG.md, study
# horse_race_calibration, one read) and is NOT run by this harness.
WINDOWS = {
    "design": {
        "start": "2022-04-01",
        "end": "2024-06-30",
        "role": "design choices only: reported, never adopts",
    },
    "seen": {
        "start": "2024-07-01",
        "end": None,
        "role": "secondary, labelled seen: reported, cannot adopt alone",
    },
    "tuesday": {
        "start": "2025-09-02",
        "end": None,
        "role": "the Tuesday-expiry regime, a separate secondary run",
    },
    "forward": {
        "start": "2026-10-22",
        "end": None,
        "role": "primary: card-ledger events logged after registration",
    },
}
PRIMARY_WINDOWS = ("forward", "vault")
POWER = {"nifty_weekly_expiries_2024_26": 115, "nifty_monthly_expiries_2024_26": 27,
         "banknifty_monthly_expiries_2024_26": 27, "card_ledger_independent_events_per_year": "about 70-110"}  # fmt: skip
ADOPTION = ("A challenger replaces the incumbent for an underlying only if, in a primary window, (1) its Brier "
            "score beats the incumbent's by DM-HLN with Holm-adjusted p < 0.05 AND (2) it passes tail coverage on "
            ">= 100 independent events: Kupiec POF at alpha = 0.10 p >= 0.05 AND Berkowitz LR p >= 0.05. Fewer "
            "than 100 events: no adoption. A smile model may not be called more accurate unless (1) holds. Every "
            "forecaster is reported whatever the outcome; nothing here changes a card's verdict.")  # fmt: skip
SCORES, TESTS, REPLAY = Scores(), Tests(), Replay()


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def registration() -> dict:
    return {"version": SPEC_VERSION, "underlyings": UNDERLYINGS, "event": EVENT_TEXT, "incumbent": INCUMBENT,
            "forecasters": FORECASTERS, "trailing_prior": TRAILING_PRIOR, "challengers": CHALLENGERS,
            "scores": asdict(SCORES), "tests": asdict(TESTS), "replay": asdict(REPLAY), "windows": WINDOWS,
            "primary_windows": PRIMARY_WINDOWS, "power": POWER, "adoption": ADOPTION}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()

"""Post-hoc addendum A6 to the card horse race (#377): the exit-rule Monte Carlo challenger of P3.5 (#388). Written and
hashed before any replay code computed the challenger on a stored session; its hash is pinned in
prereg.ADDENDUM_A6_HASH and the replay refuses to run when it differs. The registered `card-horse-race-v1`
(horserace/specs.py) and addenda A1-A4 are unchanged. Prose: evals/experiments/card_horse_race/ADDENDUM.md. A change
is a new ADDENDUM_VERSION, never an edit. The simulation settings (paths, seed, steps) and the path model's constants
(fnobook/path_pop.py, fincalc/black76.SIGMA_LO) are hashed with it.

It chains on A1-A4 (merged when it was opened): its hash covers their hashes and its race challenger set is A4's plus
p_path. A5 was registered on another branch; if it lands first, this addendum is re-chained and re-hashed before any
replay reads it.
"""

from __future__ import annotations

import hashlib
from datetime import date

from finresearch.fincalc import black76 as b
from finresearch.fnobook import path_pop as pp
from finresearch.horserace import addendum, addendum_a2, addendum_a3, addendum_a4, specs

ADDENDUM_VERSION = "card-horse-race-v1-addendum-A6"
BASE_VERSION = specs.SPEC_VERSION
CHALLENGER = "p_path"
CHALLENGERS = (*addendum_a4.CHALLENGERS, CHALLENGER)
N_PATHS = 2000  # per card in the replay: Monte Carlo se of a probability <= 0.5 / sqrt(2000) = 0.0112
SEED = 388
STEPS = ("one step per stored session after the origin up to the expiry's last session (the exchange calendar the "
         "engine already reads ahead); the last step ends at the expiry date")  # fmt: skip
ITEMS = {
    "A6.1 challenger": "p_path = P(net P&L of one set > 0 under the spec's exit plan) over N_PATHS simulated paths, "
                       "with EV = the mean net P&L and 20 dots = the net quantiles at (i + 0.5) / 20 (path_pop.DOTS, "
                       "smallest value reaching each level); every path's net is the card ledger's own replay, so the "
                       "event is the registered one. Unknown sigma, beta, leg IV or a voided path: p_path None (counted)",
    "A6.2 paths": "the card's expiry forward, zero drift (the spot drifts at r: A1.6), lognormal, starting at A1.5's "
                  "forward; sigma = the volatility of the case p_rw chose (the lower-EV of RV20 and RV60, A1.7, A1.9), "
                  "so the terminal distribution is the incumbent's and the only new element is the path; times = "
                  "calendar days / 365 from the origin (A1.6's clock), the last step to the expiry date",
    "A6.3 marks": "each leg by Black-76 at its origin IV (backtest.pricing.leg_iv of the origin reference price at A1.5's "
                  "forward and BOOK.rate) moved by sigma_t = iv + beta ln(F_t / F_0), floored at black76.SIGMA_LO, "
                  "discounted at BOOK.rate over the remaining calendar time; on the tick grid (path_pop.TICK, never "
                  "below one tick); close = settle = the mark; contracts = the leg's origin-session contracts (the "
                  "engine's exit slippage tier). The expiry session carries the expiring future at F_T only",
    "A6.4 beta": "path_pop.spot_vol_beta: OLS of the session change in iv30 / 100 on the same session's A1.7 "
                 "same-instrument return, over the last BETA_WINDOW sessions on or before the origin where both are "
                 "known; fewer than BETA_MIN pairs: unknown. iv30 is the stored 30-day ATM IV: a proxy for the ATM vol "
                 "of the card's expiry",
    "A6.5 exits": "each path replayed by fnobook.card_forecasts.simulate (backtest.engine.manage, fill same_print, the "
                  "spec's exits, costs and tiered slippage: A1.2) on path_pop.PathHistory, with a sentinel session "
                  "after the expiry date so an exit on the last session is final. No second implementation",
    "A6.6 simulation": "N_PATHS paths per card, numpy default_rng(card_seed(spec, origin)) with card_seed = the first 8 "
                       "bytes of sha256('SEED|spec|origin') mod 2^63; STEPS",
    "A6.7 Monte Carlo error": "reported per card (se of p_path sqrt(p (1 - p) / n), Wilson 95 % interval, se of EV "
                              "sd / sqrt(n), se of each exit-reason share) and over the window (mean and max se, and "
                              "the Brier inflation from simulation noise, mean p (1 - p) / n). Not a parameter "
                              "uncertainty: sigma and beta are taken as known",
    "A6.8 exit reasons": "the engine's reasons grouped as path_pop.REASONS: target (profit_take), stop, time (dte, "
                         "before_expiry_day), expiry (expiry, expiry_unsettled). The registered exit-reason score "
                         "(specs.Scores.exit_reasons): multinomial log score (eps 1e-6) of p_path's shares against the "
                         "trailing Dirichlet(1) frequencies over these four categories of earlier resolved events "
                         "(resolved strictly before the origin), same underlying and structure; reported per window, "
                         "descriptive (no test is registered on it)",
    "A6.9 race": "race.run with challengers = addendum_a4.CHALLENGERS + (p_path,); incumbent p_rw, scores, tests and "
                 "the gate unchanged; the registered Holm family (every non-incumbent forecaster x underlying) grows "
                 "by p_path",
    "A6.10 windows": "the replay windows design, seen and tuesday only, none of which can adopt; the live card's PoP is "
                     "unchanged, so adoption needs forward shadow logging or the vault, each registered separately "
                     "first",
    "A6.11 later": "marks on the P3.3 smile, paths from the P3.4 generator, bootstrap intervals over sigma and beta, "
                   "and the live card's calendar (fincalc.bustime.nse_calendar) are later addenda, registered first",
}  # fmt: skip


def card_seed(spec: str, origin: date) -> int:
    """A6.6: the card's own reproducible seed."""
    digest = hashlib.sha256(f"{SEED}|{spec}|{origin.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 2**63


def simulation() -> dict:
    return {"n_paths": N_PATHS, "seed": SEED, "steps": STEPS, "beta_window": pp.BETA_WINDOW,
            "beta_min": pp.BETA_MIN, "tick": pp.TICK, "reasons": pp.REASONS, "dots": pp.DOTS,
            "sigma_floor": b.SIGMA_LO}  # fmt: skip


def registration() -> dict:
    return {
        "version": ADDENDUM_VERSION,
        "base": BASE_VERSION,
        "base_hash": specs.registration_hash(),
        "a1_hash": addendum.addendum_hash(),
        "a2_hash": addendum_a2.addendum_hash(),
        "a3_hash": addendum_a3.addendum_hash(),
        "a4_hash": addendum_a4.addendum_hash(),
        "challengers": CHALLENGERS,
        "items": ITEMS,
        "simulation": simulation(),
    }


def addendum_hash() -> str:
    return hashlib.sha256(specs.canonical(registration()).encode()).hexdigest()

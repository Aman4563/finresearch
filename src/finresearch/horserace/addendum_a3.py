"""Post-hoc addendum A3 to the card horse race (#377): the forward/IV challenger of P3.2 (#385). Written and hashed
before any replay code computed the challenger on a stored session; its hash is pinned in prereg.ADDENDUM_A3_HASH
and the replay refuses to run when it differs. The registered `card-horse-race-v1` (horserace/specs.py) and addenda
A1 and A2 are unchanged. Prose: evals/experiments/card_horse_race/ADDENDUM.md. A change is a new ADDENDUM_VERSION,
never an edit. The engine's registered numbers (fincalc/black76.py tolerances, band and limits) are hashed with it.
"""

from __future__ import annotations

import hashlib

from finresearch.fincalc import black76 as b
from finresearch.fincalc import fno_history as fh
from finresearch.horserace import addendum, addendum_a2, specs

ADDENDUM_VERSION = "card-horse-race-v1-addendum-A3"
BASE_VERSION = specs.SPEC_VERSION
CHALLENGER = "p_rn_b76"
CHALLENGERS = (*addendum_a2.CHALLENGERS, CHALLENGER)
ITEMS = {
    "A3.1 challenger": "p_rn_b76 = the lognormal PoP, EV and 20 dots (A1.9) at the ATM IV of A3.4, with A1.6's spot, "
                      "t, rate, drift, costs, legs and fills unchanged: it differs from p_rn_atm only in the ATM IV "
                      "(forward, discount factor, price filter and solver). Unknown IV: p_rn_b76 None (counted)",
    "A3.2 forward": "F_par = black76.parity_fit on the card's expiry at the origin close over every strike whose call "
                    "AND put have a market settle (fno_history.market_settle: >= MIN_CONTRACTS contracts and settle = "
                    "close), equal weights (the bhavcopy has no bid/ask), Huber IRLS, |ln K/F| <= PARITY_BAND; fewer "
                    "than MIN_STRIKES strikes or a non-negative slope: unknown. Neither the future nor one strike",
    "A3.3 discount factor": "DF = the regression's own discount factor when identified (se of r_impl <= R_SE_MAX and "
                            "r_impl inside R_IMPL_RANGE); else unknown and so is p_rn_b76. No dated rate is "
                            "substituted (FBIL dated rates are not collected while the owner's data-rights decision "
                            "on #447 is open) and never BOOK.rate's 6.5 %",
    "A3.4 ATM IV": "black76.atm_iv over the market settles (OptionQuote price = settle; spread band unknown) at the "
                   "listed strike nearest F_par: the mean of the call and put IVs, either alone. T = A1.6's calendar t: "
                   "with DF given directly a price depends on sigma^2 T only, so the total variance (and the PoP) is "
                   "the same on any clock (A2.6's argument)",
    "A3.5 solver": "black76.implied_vol: Newton safeguarded by bisection (rtsafe) inside [SIGMA_LO, SIGMA_HI], "
                   "converged at PRICE_TOL relative to the out-of-the-money twin's price or SIGMA_TOL bracket width, "
                   "an in-the-money side solved through parity on its OTM twin; a price on or outside the no-arbitrage "
                   "bounds or the bracket is unknown with a reason (never the bracket's edge)",
    "A3.6 logged": "per card: F_par, its basis (F_par - F_A1.5) / F_A1.5 to the A1.5 forward, r_impl, se(F), the "
                   "strikes used and the reason when unknown. Reported, not scored",
    "A3.7 race": "race.run with challengers = addendum_a2.CHALLENGERS + (p_rn_b76,); incumbent p_rw, scores, tests and "
                 "the gate unchanged; the registered Holm family (every non-incumbent forecaster x underlying) grows "
                 "by p_rn_b76",
    "A3.8 windows": "the replay windows design, seen and tuesday only, none of which can adopt; the live card is "
                    "unchanged, so adoption needs forward shadow logging or the vault, each registered separately first",
    "A3.9 later": "IVs from recorded bid/ask mids (option_quote, #363) with spread weights, error bands and the issue's "
                  "acceptance (a)-(e) on >= 20 recorded sessions are a later addendum, registered before they are read",
}  # fmt: skip


def engine_constants() -> dict:
    return {"SIGMA_LO": b.SIGMA_LO, "SIGMA_HI": b.SIGMA_HI, "PRICE_TOL": b.PRICE_TOL, "SIGMA_TOL": b.SIGMA_TOL,
            "MAX_ITER": b.MAX_ITER, "MAX_DF": b.MAX_DF, "PARITY_BAND": b.PARITY_BAND, "MIN_STRIKES": b.MIN_STRIKES,
            "HUBER_C": b.HUBER_C, "HUBER_ITER": b.HUBER_ITER, "BAND_PASSES": b.BAND_PASSES,
            "R_IMPL_RANGE": list(b.R_IMPL_RANGE), "R_SE_MAX": b.R_SE_MAX, "TICK": b.TICK, "MAX_BAND": b.MAX_BAND,
            "MARKET_SETTLE_MIN_CONTRACTS": fh.MIN_CONTRACTS}  # fmt: skip


def registration() -> dict:
    return {
        "version": ADDENDUM_VERSION,
        "base": BASE_VERSION,
        "base_hash": specs.registration_hash(),
        "a1_hash": addendum.addendum_hash(),
        "a2_hash": addendum_a2.addendum_hash(),
        "challengers": CHALLENGERS,
        "items": ITEMS,
        "engine": engine_constants(),
    }


def addendum_hash() -> str:
    return hashlib.sha256(specs.canonical(registration()).encode()).hexdigest()

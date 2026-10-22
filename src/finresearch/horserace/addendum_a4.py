"""Post-hoc addendum A4 to the card horse race (#377): the SVI risk-neutral density challenger of P3.3 (#386). Written
and hashed before any replay code computed the challenger on a stored session; its hash is pinned in
prereg.ADDENDUM_A4_HASH and the replay refuses to run when it differs. The registered `card-horse-race-v1`
(horserace/specs.py) and addenda A1-A3 are unchanged. Prose: evals/experiments/card_horse_race/ADDENDUM.md. A change
is a new ADDENDUM_VERSION, never an edit. The fit's and the density's registered numbers (fincalc/svi.py and
fincalc/smile.py) are hashed with it; A3's hash carries the Black-76 engine's.
"""

from __future__ import annotations

import hashlib

from finresearch.fincalc import smile as sm
from finresearch.fincalc import svi
from finresearch.horserace import addendum, addendum_a2, addendum_a3, specs

ADDENDUM_VERSION = "card-horse-race-v1-addendum-A4"
BASE_VERSION = specs.SPEC_VERSION
CHALLENGER = "p_rn_svi"
CHALLENGERS = (*addendum_a3.CHALLENGERS, CHALLENGER)
ITEMS = {
    "A4.1 challenger": "p_rn_svi = PoP, EV and 20 dots (A1.9) of holding the legs to expiry with S_T = F e^k, k drawn "
                       "from the SVI risk-neutral density of item A4.3 (smile.outcome at OUT_STEPS cells for PoP and "
                       "EV, smile.pnl_distribution at 2,000 cells for the dots), F = the A1.5 forward (spot e^(rate t)); "
                       "A1.6's spot, t, rate, costs, legs and fills unchanged. It differs from p_rn_b76 only in the "
                       "shape of the terminal distribution (the smile's density in place of the lognormal at the ATM "
                       "IV). Unknown smile: p_rn_svi None (counted by reason)",
    "A4.2 inputs": "A3.2's F_par and A3.3's DF on the card's expiry at the origin close (unknown: unknown); the points "
                   "are black76.otm_ivs over the market settles (fno_history.market_settle; OptionQuote price = "
                   "settle, spread band unknown) with k = ln(K/F_par), equal weights in volatility (settles carry no "
                   "bid/ask); T = A1.6's calendar t (one slice's total variance is the same on any clock, A2.6)",
    "A4.3 fit": "svi.fit: raw SVI by Zeliade's quasi-explicit method (exact constrained least squares in (a, d, c) "
                "over b >= 0, |rho| <= RHO_MAX, b (1 + |rho|) <= 2; (m, s) by Nelder-Mead from the best of a "
                "START_GRID x START_GRID grid). Refused (unknown, with the reason) when fewer than MIN_POINTS strikes, "
                "an IV RMSE above MAX_RMSE, or any static violation on the check grid (min w <= 0, Lee's bound, "
                "g(k) < 0): never repaired or clipped. Raw SVI for NIFTY and BANKNIFTY (SSVI is for the sparse chains "
                "the race does not include)",
    "A4.4 logged": "per card: the raw parameters, n, IV RMSE, the fit flags, RR25 and BF25 (smile.rr_bf), the quotes "
                   "dropped by reason and the reason when unknown. Reported, not scored",
    "A4.5 race": "race.run with challengers = addendum_a3.CHALLENGERS + (p_rn_svi,); incumbent p_rw, scores, tests and "
                 "the gate unchanged; the registered Holm family (every non-incumbent forecaster x underlying) grows "
                 "by p_rn_svi. Smile models may not be called more accurate unless Brier improves at Holm p < 0.05",
    "A4.6 windows": "the replay windows design, seen and tuesday only, none of which can adopt; the live card is "
                    "unchanged, so adoption needs forward shadow logging or the vault, each registered separately first",
    "A4.7 not claimed": "issue #386's start trigger (MF16: flat-IV strike picks or PoP change on >= 10 % of verdicts) "
                        "is not established by this registration; the issue's measured B-L vs flat difference (<= 2.4 "
                        "points at 10-25 delta) was measured on 2022-26, so the design and seen windows are not "
                        "independent of it",
    "A4.8 later": "the issue's acceptance (a)-(e) (fit RMSE and violations, every-third-strike hold-out against linear "
                  "interpolation, EV_Q at the mids, B-L logged in P2.1, P&L explain) on recorded mids (#363) are a "
                  "later addendum, registered before they are read",
}  # fmt: skip


def engine_constants() -> dict:
    return {"K_WIDE": svi.K_WIDE, "K_LOCAL": svi.K_LOCAL, "GRID_POINTS": svi.GRID_POINTS, "LEE_MAX": svi.LEE_MAX,
            "CAL_TOL": svi.CAL_TOL, "RHO_MAX": svi.RHO_MAX, "MIN_POINTS": svi.MIN_POINTS, "S_MIN": svi.S_MIN,
            "S_MAX": svi.S_MAX, "M_PAD": svi.M_PAD, "START_GRID": svi.START_GRID, "NM_STEP": svi.NM_STEP,
            "NM_ITER": svi.NM_ITER, "NM_FTOL": svi.NM_FTOL, "MAX_RMSE": svi.MAX_RMSE, "TARGET_RMSE": svi.TARGET_RMSE,
            "OUT_SD": sm.OUT_SD, "OUT_STEPS": sm.OUT_STEPS, "DELTA": sm.DELTA, "DELTA_TOL": sm.DELTA_TOL}  # fmt: skip


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
        "engine": engine_constants(),
    }


def addendum_hash() -> str:
    return hashlib.sha256(specs.canonical(registration()).encode()).hexdigest()

"""Post-hoc addendum A2 to the card horse race (#377): the business-time clock challenger of P3.1 (#384). Written and
hashed before any replay code computed the challenger on a stored session; its hash is pinned in
prereg.ADDENDUM_A2_HASH and the replay refuses to run when it differs. The registered `card-horse-race-v1`
(horserace/specs.py) and addendum A1 are unchanged. Prose: evals/experiments/card_horse_race/ADDENDUM.md.
A change is a new ADDENDUM_VERSION, never an edit. The clock's registered numbers are hashed with it.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict

from finresearch.fincalc import bustime as bt
from finresearch.horserace import addendum, specs

ADDENDUM_VERSION = "card-horse-race-v1-addendum-A2"
BASE_VERSION = specs.SPEC_VERSION
CHALLENGER = "p_rw_bus"
CHALLENGERS = (*specs.CHALLENGERS, CHALLENGER)
ITEMS = {
    "A2.1 challenger": "p_rw_bus = p_rw (the lower-EV of the RV20 and RV60 lognormal cases, A1.6, A1.7, A1.9 "
                       "unchanged) with each volatility put on the business clock: sigma_eff = "
                       "bustime.rescale_vol(RV_N, T_bus, t) = RV_N x sqrt(T_bus / t), so the total variance to "
                       "expiry is RV_N^2 x T_bus; t (calendar days / 365), spot, rate, drift, costs, legs and fills "
                       "are A1.6's. Unknown T_bus or RV_N: that case is unknown (p_rw_bus None when both are)",
    "A2.2 clock": "T_bus = bustime.business_years(expiry, 15:30 IST on the origin session, calendar, PRIOR, "
                  "events=EXCLUDED): sessions after the origin through the expiry session x (1 + w_o) + w_w x "
                  "weekend and holiday days, over Y = 252 + 252 w_o + 113 w_w; PRIOR w_o = 0 (the session is the "
                  "whole close-to-close day), w_w = 0.10 of one, Y = 263.3. RV_N = sd(close-to-close) x sqrt(252) "
                  "is sigma per business year because a calendar year of the clock sums to 1",
    "A2.3 weights": "the registered prior, NOT estimated on any FinResearch data: the issue's fallback is 0.1-0.3 of "
                    "an open-to-close session for a weekend or holiday day; 0.10 of a close-to-close day is "
                    "0.10-0.14 of a session for w_o in 0-0.4. Consistent with French and Roll (1986, JFE 17, 5-26) "
                    "and Fortune (1998, FRB Boston WP 98-6): a whole weekend's variance about 10-20 % above one "
                    "weekday's (US data, not an Indian estimate)",
    "A2.4 events": "event variance is excluded by this registration (P3.8 is not built): EXCLUDED, which is not a "
                   "claim that no event falls in the window",
    "A2.5 calendar": "the replay's calendar is bustime.Calendar.from_sessions over the underlying's stored sessions "
                     "from the warm-up start to the last stored session (the session list the replay already uses "
                     "for expiry days): a weekday with no stored session is a holiday. NSE publishes each year's "
                     "trading holidays before the year starts, so only an unscheduled closure is look-ahead. A "
                     "missing stored weekday cannot be told from a holiday: the replay reports how many such "
                     "weekdays there are. An expiry after the last stored session or on a non-session: T_bus unknown",
    "A2.6 no risk-neutral twin": "no p_rn_atm_bus: an ATM IV solved and used on the same clock gives the same total "
                                 "variance at expiry, so its PoP would be p_rn_atm's",
    "A2.7 race": "race.run with challengers = specs.CHALLENGERS + (p_rw_bus,); incumbent p_rw, scores, tests and the "
                 "gate unchanged; the registered Holm family (every non-incumbent forecaster x underlying) grows by "
                 "p_rw_bus",
    "A2.8 windows": "the replay windows design, seen and tuesday only, none of which can adopt; the forward card "
                    "ledger has no p_rw_bus (the live card is unchanged), so adoption needs forward shadow logging "
                    "or the vault, each registered separately first",
    "A2.9 later": "estimated weights (w_o by bustime.overnight_weight, w_w by bustime.nontrading_weight on "
                  "2022-01-03 to 2024-06-30 only, issue #384 (a)) would be a new addendum registered before any "
                  "window after 2024-06-30 is read with them",
}  # fmt: skip


def registration() -> dict:
    return {
        "version": ADDENDUM_VERSION,
        "base": BASE_VERSION,
        "base_hash": specs.registration_hash(),
        "a1_hash": addendum.addendum_hash(),
        "challengers": CHALLENGERS,
        "items": ITEMS,
        "clock": {
            "prior": {k: v for k, v in asdict(bt.PRIOR).items() if k != "source"},
            "session": [bt.SESSION_OPEN, bt.SESSION_CLOSE, bt.SESSION_MINUTES],
            "per_year": [bt.SESSIONS_PER_YEAR, bt.OVERNIGHTS_PER_YEAR, bt.NONTRADING_PER_YEAR],
        },
    }


def addendum_hash() -> str:
    return hashlib.sha256(specs.canonical(registration()).encode()).hexdigest()

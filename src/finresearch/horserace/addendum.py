"""Post-hoc addendum A1 to the card horse race (#377): what the 2022-26 replay needs that the registered
`card-horse-race-v1` (horserace/specs.py, unchanged) leaves open. Written and hashed before any replay code read a
stored session; its hash is pinned in prereg.ADDENDUM_HASH and the replay refuses to run when they differ.
Prose: evals/experiments/card_horse_race/ADDENDUM.md. A change is a new ADDENDUM_VERSION, never an edit.
"""

from __future__ import annotations

import hashlib

from finresearch.horserace import specs

ADDENDUM_VERSION = "card-horse-race-v1-addendum-A1"
BASE_VERSION = specs.SPEC_VERSION
ITEMS = {
    "A1.1 entry fill": "each leg at backtest.pricing.fill(settle, side, contracts that session, BOOK): the "
                       "registered settlement price moved by the engine's tiered slippage (the event is after "
                       "slippage); a leg with no settlement price (None or <= 0) means no card that session (counted)",
    "A1.2 exits": "fnobook.card_forecasts.simulate, i.e. backtest.engine.manage with fill='same_print' (the "
                  "registered backtest mode; #432 added the parameter after registration), the spec's own exits "
                  "(fincalc.tradeplan.spec_exits), statutory costs and the tiered slippage",
    "A1.3 one set": "one lot per leg (the registered index specs are 1:1); no sizing, margin, max_concurrent, "
                    "max_per_expiry, min_days_between or IV-percentile entry filter: one card per session, so the "
                    "replay is NOT the recipe's trade list (the weekly condor's recipe takes far fewer sessions)",
    "A1.4 expiry": "as backtest.engine.try_enter: the nearest expiry with spec.dte_min <= DTE <= spec.dte_max (monthly "
                   "specs: an expiry with a future); no card when a spec that must exit before expiry day has no "
                   "session left before it (next session >= expiry day)",
    "A1.5 legs": "backtest.engine.choose_legs(spec, day, expiry, forward, BOOK) at the origin close, forward by "
                 "backtest.pricing.forward; no card when it refuses (counted by reason) or the lot size is unknown",
    "A1.6 model inputs": "spot = backtest.pricing.discounted_spot(forward, t, BOOK.rate); t = calendar days to "
                         "expiry / 365; rate and drift = BOOK.rate; legs at the A1.1 fills; costs = the entry "
                         "statutory charges at the fills + the expected STT on exercised in-the-money longs (the "
                         "card's exit cost); no further spread (the slippage is already in the fills; the expected "
                         "exit slippage of an early exit is NOT in the model EV)",
    "A1.7 realised volatility": "log returns between consecutive stored sessions on the SAME instrument: the "
                                "bhavcopy's underlying value when both sessions have it, else the nearest-expiry "
                                "future present on both (no splice across the 2024 switch or a roll); RV_N = sample "
                                "sd of the last N returns x sqrt(252), N = 20 and 60, unknown with fewer than N "
                                "returns; warm-up from 400 calendar days before the first origin (the engine's)",
    "A1.8 ATM IV": "the mean Black-76 IV (backtest.pricing.leg_iv) of the call and put at the listed strike nearest "
                   "the forward on the card's expiry, either alone if only one solves, else unknown (p_rn_atm None)",
    "A1.9 forecasters": "fincalc.options.outcome on 4,000 steps for each PoP and EV; dots = 20 quantiles "
                        "(i + 0.5) / 20 of fincalc.options.pnl_distribution on 2,000 steps, per model; p_rw = the "
                        "lower-EV of p_rw20 and p_rw60 (p_rw20 alone when RV60 is unknown), as the card",
    "A1.10 gate proxy": "gate_pass = the spec's registered IV-percentile entry filter at the origin (iv30 percentile "
                        "over the stored past year >= spec.iv_pct_min, as try_enter); None for specs without one "
                        "(the debit controls) or an unknown percentile. A PROXY: the live card's verdict needs "
                        "bid/ask, budget and saved-backtest evidence the bhavcopy does not have",
    "A1.11 resolution": "outcome and Rs P&L per set by A1.2; void and not-yet-finished cards are counted, never "
                        "events; resolved_on = the expiry date (conservative: delays the trailing rates, never leaks)",
    "A1.12 independence": "first = the first session per (underlying, expiry, structure) whose card resolved",
    "A1.13 windows": "events from 2022-04-01 to the last stored session; the replay serves design, seen and tuesday "
                     "only (forward is registered as card-ledger events and is refused for the replay)",
}  # fmt: skip


def registration() -> dict:
    return {
        "version": ADDENDUM_VERSION,
        "base": BASE_VERSION,
        "base_hash": specs.registration_hash(),
        "items": ITEMS,
    }


def addendum_hash() -> str:
    return hashlib.sha256(specs.canonical(registration()).encode()).hexdigest()

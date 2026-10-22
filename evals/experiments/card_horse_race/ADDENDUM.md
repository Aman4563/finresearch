# Addendum A1 (post hoc): the 2022-26 card replay (#377, second slice)

Recorded 22-Oct-2026, after `card-horse-race-v1` was registered (PREREG.md, unchanged) and before any replay code
read a stored session or an outcome. These are the choices the registration's `Replay` section leaves open; they are
**post hoc** relative to the registration, written down before the replay was built or run. The registered spec and
its hash (`11f10c09…4701060`) are untouched. The addendum is `src/finresearch/horserace/addendum.py`
(`card-horse-race-v1-addendum-A1`), sha256 `d26e9f0d58a8f86cb66433f59053633a98efdab2f824c7ee7112f76c4e3bd3a6`, pinned in `horserace/prereg.py`
(`ADDENDUM_HASH`); the replay refuses to run when either hash differs. Any change is a new addendum version.

## Items (each can be vetoed by the owner as a new addendum version)

1. **Entry fill.** Each leg at `backtest.pricing.fill(settle, side, contracts, BOOK)`: the registered settlement
   price moved by the engine's tiered slippage (the event is after slippage). A leg with no settlement price: no card.
2. **Exits.** `fnobook.card_forecasts.simulate`, i.e. `backtest.engine.manage` with `fill="same_print"` (the
   registered backtest mode; #432 added the parameter after registration), the spec's own exits, costs, slippage.
3. **One set per session.** One lot per leg; no sizing, margin, concurrency, spacing or IV-percentile filter. The
   replay is therefore not the recipe's trade list (the weekly condor's recipe takes far fewer sessions).
4. **Expiry.** As `engine.try_enter`: the nearest expiry in the spec's DTE window (monthly specs: an expiry with a
   future); no card when a spec that must exit before expiry day has no session left before it.
5. **Legs.** `engine.choose_legs` at the origin close with `pricing.forward`; a refusal or an unknown lot size means no
   card (counted by reason).
6. **Model inputs.** spot = the discounted forward; t = calendar days / 365; rate = drift = `BOOK.rate`; legs at the
   fills; costs = entry charges at the fills + expected exercise STT on in-the-money longs. The expected exit slippage
   of an early exit is NOT in the model EV.
7. **Realised volatility.** Log returns between consecutive sessions on the same instrument (the underlying value
   when both sessions have it, else the nearest future present on both: no splice across the 2024 switch or a roll);
   RV_N = sample sd of the last N returns x sqrt(252), N = 20, 60; unknown below N returns; 400-day warm-up.
8. **ATM IV.** The mean Black-76 IV of the call and put at the listed strike nearest the forward (either alone),
   else unknown: `p_rn_atm` None.
9. **Forecasters.** `fincalc.options.outcome` (4,000 steps) for PoP and EV; 20 dots of `pnl_distribution` (2,000
   steps) per model; `p_rw` = the lower-EV of `p_rw20` and `p_rw60` (`p_rw20` alone when RV60 is unknown).
10. **Gate proxy.** `gate_pass` = the spec's registered IV-percentile entry filter at the origin (None for the debit
    controls or an unknown percentile). It is a proxy: the live card's verdict needs bid/ask, budget and backtest
    evidence the bhavcopy does not hold. `p_gate` and gate information in the replay are about this proxy.
11. **Resolution.** Void and unfinished cards are counted, never events; `resolved_on` = the expiry date
    (conservative: it delays the trailing rates and never leaks).
12. **Independence.** `first` = the first session per (underlying, expiry, structure) whose card resolved.
13. **Windows.** Events from 2022-04-01 to the last stored session; the replay serves `design`, `seen` and
    `tuesday` only (`forward` is registered as card-ledger events and is refused for the replay).

# Addendum A2 (post hoc): the business-time clock challenger `p_rw_bus` (P3.1, #384)

Recorded 22-Oct-2026, after `card-horse-race-v1` and addendum A1 (both unchanged) and before any replay code computed
the challenger on a stored session or read an outcome. It is `src/finresearch/horserace/addendum_a2.py`
(`card-horse-race-v1-addendum-A2`), sha256 `a43dd5c2bd94d48e304e84ca7da391d7841f810de8e261eb12c12addf37022c0`, pinned in `horserace/prereg.py` (`ADDENDUM_A2_HASH`); the hash
covers the clock's registered numbers (`fincalc/bustime.py`: the prior weights, session hours, per-year counts). The
replay refuses to run when any of the three hashes differs. Any change is a new addendum version.

1. **Challenger.** `p_rw_bus` = `p_rw` (the lower-EV of the RV20 and RV60 lognormal cases, A1.6, A1.7, A1.9) with
   each volatility moved to the business clock: sigma_eff = RV_N x sqrt(T_bus / t), so the total variance to expiry
   is RV_N^2 x T_bus. t (calendar days / 365), spot, rate, drift, costs, legs and fills stay A1.6's.
2. **Clock.** `bustime.business_years` from 15:30 IST on the origin session to 15:30 IST on the expiry:
   T_bus = [n_s + w_o n_o + w_w n_w] / Y with n_o = n_s from a close, Y = 252 + 252 w_o + 113 w_w (the issue writes 251 overnights; 252 makes a
   calendar year exactly 1). A calendar year of the clock sums to 1, so RV_N = sd(close-to-close) x sqrt(252) is
   already sigma per business year.
3. **Weights: the registered prior, not estimated.** w_o = 0 (the session is the whole close-to-close day) and
   w_w = 0.10 of one per weekend or holiday day (Y = 263.3). The issue's registered fallback is 0.1-0.3 of an
   open-to-close session; 0.10 of a close-to-close day is 0.10-0.14 of a session for any w_o from 0 to 0.4.
   Consistent with, not taken from, US evidence that a whole weekend's variance is only about 10-20 % above one
   weekday's (French and Roll 1986, JFE 17, 5-26; Fortune 1998, Federal Reserve Bank of Boston WP 98-6, pp. 1, 3).
   No FinResearch data was used.
4. **Events** are excluded by this registration (P3.8 is not built). That is not a claim that no event falls in the
   window.
5. **Calendar.** The underlying's stored sessions (the list the replay already uses for expiry days): a weekday with
   no stored session is a holiday. NSE publishes the year's holidays before the year starts, so only an unscheduled
   closure is look-ahead. A missing stored weekday cannot be told from a holiday; the replay reports how many there
   are. An expiry after the last stored session or on a non-session gives an unknown T_bus.
6. **No risk-neutral twin.** An ATM IV solved and used on the same clock has the same total variance at expiry, so
   a `p_rn_atm_bus` would equal `p_rn_atm`.
7. **Race.** Challengers = `specs.CHALLENGERS` + `p_rw_bus`; incumbent, scores, tests and the gate unchanged. The
   registered Holm family (every non-incumbent forecaster x underlying) grows by `p_rw_bus`.
8. **Windows.** Replay windows only (design, seen, tuesday), none of which can adopt. The forward card ledger has no
   `p_rw_bus` because the live card is unchanged: adoption needs forward shadow logging or the vault, each
   registered first.
9. **Later.** Weights estimated by `bustime.overnight_weight` and `bustime.nontrading_weight` on 2022-01-03 to
   2024-06-30 only (#384 acceptance (a)) would be a new addendum, registered before any later window is read with
   them.

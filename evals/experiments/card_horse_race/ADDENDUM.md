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

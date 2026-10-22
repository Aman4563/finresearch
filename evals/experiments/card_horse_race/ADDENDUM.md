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

# Addendum A3 (post hoc): the forward and Black-76 IV challenger `p_rn_b76` (P3.2, #385)

Recorded 22-Oct-2026, after `card-horse-race-v1` and addenda A1 and A2 (all unchanged) and before any replay code
computed the challenger on a stored session or read an outcome. It is `src/finresearch/horserace/addendum_a3.py`
(`card-horse-race-v1-addendum-A3`), sha256 `c5c07f159a6a8154ac7f264a847645fd4556b69c86fd1e1808bf8bd9b64dc10b`, pinned in `horserace/prereg.py` (`ADDENDUM_A3_HASH`); the
hash covers the engine's registered numbers (`fincalc/black76.py`: solver bracket and tolerances, parity band,
minimum strikes, Huber constant, implied-rate plausibility limits, tick and maximum band) and the market-settle
contract minimum. The replay refuses to run when any of the four hashes differs. Any change is a new addendum version.

1. **Challenger.** `p_rn_b76` = the lognormal PoP, EV and 20 dots (A1.9) at the ATM IV of item 4, with A1.6's spot,
   t, rate, drift, costs, legs and fills unchanged, so it differs from `p_rn_atm` only in the ATM IV (forward,
   discount factor, price filter and solver). An unknown IV gives `p_rn_b76` None, counted.
2. **One forward per expiry.** F_par = `black76.parity_fit` on the card's expiry at the origin close: the
   regression C - P = DF F - DF K over every strike whose call and put both have a market settle
   (`fno_history.market_settle`: >= 10 contracts and settle = close) with |ln K/F| <= 5 %, equal weights (the
   bhavcopy has no bid/ask), Huber IRLS (c = 1.345). Fewer than 3 strikes or a non-negative slope: unknown. It is
   neither the future nor one strike.
3. **Discount factor.** The regression's own DF when it is identified (standard error of r_impl <= 1 rate point and
   r_impl between -2 % and 20 %); otherwise unknown, and so is `p_rn_b76`. No dated rate is substituted (FBIL dated
   rates are not collected while the owner's data-rights decision on #447 is open), and never `BOOK.rate`'s 6.5 %.
4. **ATM IV.** `black76.atm_iv` over the market settles at the listed strike nearest F_par: the mean of the call and
   put IVs, either alone. T = A1.6's calendar t: with DF given directly a price depends on sigma^2 T only, so the
   total variance and the PoP are the same on any clock (A2.6's argument).
5. **Solver.** `black76.implied_vol`: Newton safeguarded by bisection inside [0.01 %, 1,000 %], converged at
   1e-10 relative to the out-of-the-money twin's price or a 1e-10 bracket; an in-the-money side is solved on its OTM
   twin through parity; a price on or outside the no-arbitrage bounds or the bracket is unknown with a reason.
6. **Logged, not scored.** Per card: F_par, its basis to the A1.5 forward, r_impl, se(F), strikes used, and the
   reason when unknown.
7. **Race.** Challengers = addendum A2's + `p_rn_b76`; incumbent `p_rw`, scores, tests and the gate unchanged. The
   registered Holm family (every non-incumbent forecaster x underlying) grows by `p_rn_b76`.
8. **Windows.** Replay windows only (design, seen, tuesday), none of which can adopt. The live card is unchanged:
   adoption needs forward shadow logging or the vault, each registered first.
9. **Later.** IVs from recorded bid/ask mids (`option_quote`, #363) with spread weights, error bands and the issue's
   acceptance checks (a)-(e) on >= 20 recorded sessions are a later addendum, registered before they are read.

# Addendum A4 (post hoc): the SVI risk-neutral density challenger `p_rn_svi` (P3.3, #386)

Recorded 22-Oct-2026, after `card-horse-race-v1` and addenda A1-A3 (all unchanged) and before any replay code
computed the challenger on a stored session or read an outcome. It is `src/finresearch/horserace/addendum_a4.py`
(`card-horse-race-v1-addendum-A4`), sha256 `3602512dc19256374975c5f8779a7890ea16eb15d5471871be056a6684cbd6da`, pinned in `horserace/prereg.py` (`ADDENDUM_A4_HASH`); the
hash covers A3's hash (and so the Black-76 engine's constants) and the fit's and density's registered settings
(`fincalc/svi.py`: check grid, Lee bound, rho limit, minimum points, the (m, s) box, start grid, Nelder-Mead step,
iterations and tolerance, RMSE refusal and target; `fincalc/smile.py`: the outcome grid's width and cells, the delta
and its tolerance). The replay refuses to run when any of the five hashes differs. Any change is a new addendum version.

1. **Challenger.** `p_rn_svi` = PoP, EV and 20 dots (A1.9) of holding the legs to expiry with S_T = F e^k, k drawn
   from the SVI risk-neutral density of item 3 (`smile.outcome`, 4,000 cells over +/- 12 ATM standard deviations,
   PoP from the smile's exact CDF; `smile.pnl_distribution`, 2,000 cells, for the dots). F = the A1.5 forward
   (spot e^(rate t)); A1.6's spot, t, rate, costs, legs and fills are unchanged, so it differs from `p_rn_b76` only
   in the shape of the terminal distribution. An unknown smile gives `p_rn_svi` None, counted by reason.
2. **Inputs.** A3.2's F_par and A3.3's DF on the card's expiry at the origin close (unknown: unknown). The points are
   `black76.otm_ivs` over the market settles (out-of-the-money sides plus both sides at the strike nearest F_par),
   k = ln(K/F_par), equal weights in volatility (settles carry no bid/ask). T = A1.6's calendar t: one slice's total
   variance w(k) is the same on any clock (A2.6's argument).
3. **Fit.** `svi.fit`: raw SVI by Zeliade's quasi-explicit method, the exact constrained least squares in (a, d, c)
   over b >= 0, |rho| <= 0.999 and Lee's b (1 + |rho|) <= 2, then (m, s) by Nelder-Mead from the best point of a
   9 x 9 grid. Refused, unknown with the reason, when there are fewer than 6 strikes, the IV RMSE is above 1 vol
   point, or the smile has any static violation on the check grid (min w <= 0, Lee's bound, g(k) < 0, Gatheral and
   Jacquier 2014): never repaired or clipped. The engine reproduces their published SVI-JW values for Vogt's
   arbitrageable smile and refuses it. Raw SVI for NIFTY and BANKNIFTY; SSVI is for the sparse chains the race
   does not include.
4. **Logged, not scored.** Per card: the raw parameters, n, IV RMSE, fit flags, RR25 and BF25, the quotes dropped
   by reason, and the reason when unknown.
5. **Race.** Challengers = addendum A3's + `p_rn_svi`; incumbent `p_rw`, scores, tests and the gate unchanged. The
   registered Holm family (every non-incumbent forecaster x underlying) grows by `p_rn_svi`. Smile models may not
   be called more accurate unless Brier improves at Holm p < 0.05 (#377).
6. **Windows.** Replay windows only (design, seen, tuesday), none of which can adopt. The live card is unchanged:
   adoption needs forward shadow logging or the vault, each registered first.
7. **Not claimed.** Issue #386's start trigger (MF16: flat-IV strike picks or PoP change on at least 10 % of
   verdicts) is not established by this registration. The issue's measured difference between Breeden-Litzenberger
   and flat probabilities (at most 2.4 points at 10-25 delta) came from 2022-26, so the design and seen windows are
   not independent of it.
8. **Later.** The issue's acceptance checks (a)-(e) on recorded mids (#363) are a later addendum, registered before
   they are read.

# Addendum A6 (post hoc): the exit-rule Monte Carlo challenger `p_path` (P3.5, #388)

Recorded 22-Oct-2026, after `card-horse-race-v1` and addenda A1-A4 (all unchanged) and before any replay code
computed the challenger on a stored session or read an outcome. It is `src/finresearch/horserace/addendum_a6.py`
(`card-horse-race-v1-addendum-A6`), sha256 `18f7b40274feef48aca3760e8244a37d1c650118fc9dcd1c8b2c115ba244881b`, pinned in `horserace/prereg.py`
(`ADDENDUM_A6_HASH`); the hash covers the simulation settings (2,000 paths per card, seed 388 and the per-card seed
rule, one step per stored session) and the path model's constants (`fnobook/path_pop.py`: the beta window and
minimum, the tick, the exit-reason grouping, the dot levels; `black76.SIGMA_LO`). The replay refuses to run when any
of the six hashes differs. Any change is a new addendum version. It chains on A1-A4 (A4 merged before this was
opened); A5 was being registered on another branch and is not covered.

The card shows P(P&L > 0 at expiry), but the plan exits early (target, stop, a days-to-expiry exit, the last session
before expiry day). This challenger is the same event under the plan's own exits, simulated.

1. **Challenger.** `p_path` = P(net P&L of one set > 0 under the spec's exit plan) over 2,000 simulated paths; EV =
   the mean net P&L; 20 dots = the net quantiles at (i + 0.5) / 20. Each path's net is the card ledger's own replay,
   so the event is the registered one. An unknown sigma, beta or leg IV, or a voided path: `p_path` None, counted.
2. **Paths.** The card's expiry forward, zero drift (the spot drifts at r, A1.6), lognormal from A1.5's forward, at the
   volatility of the case `p_rw` chose (the lower-EV of RV20 and RV60, A1.7, A1.9). The terminal distribution is
   therefore the incumbent's: the only new element is the path. Times are calendar days / 365 from the origin
   (A1.6's clock); the last step ends at the expiry date.
3. **Marks.** Each leg by Black-76 at its origin IV (`backtest.pricing.leg_iv` of the origin reference price, A1.5's
   forward, `BOOK.rate`) moved by sigma_t = iv + beta ln(F_t / F_0), floored at `black76.SIGMA_LO`, discounted at
   `BOOK.rate` over the remaining calendar time, on the Rs 0.05 tick grid (never below one tick). close = settle =
   the mark; contracts = the leg's origin-session contracts, which pick the engine's exit slippage tier (the
   half-spread). The expiry session carries only the expiring future at F_T, which the engine settles at.
4. **Spot-vol slope.** beta = OLS of the session change in iv30 / 100 on the same session's A1.7 return, over the last
   250 sessions on or before the origin where both are known; fewer than 120 pairs: unknown. iv30 (the stored 30-day
   ATM IV) is a proxy for the ATM vol of the card's expiry.
5. **Exits: no second implementation.** Every path is replayed by `fnobook.card_forecasts.simulate` (A1.2:
   `backtest.engine.manage`, `fill="same_print"`, the spec's exits, costs and tiered slippage) on
   `path_pop.PathHistory`, with a sentinel session after the expiry date so an exit on the last session is final.
   `tests/test_path_pop.py` checks a known path against the replay of the same quotes (identical reason, day and net).
6. **Simulation.** 2,000 paths per card; `numpy.random.default_rng(card_seed(spec, origin))`, card_seed = the first 8
   bytes of sha256("388|spec|origin") mod 2^63; one step per stored session after the origin up to the expiry's last
   session (the exchange calendar, which the engine already reads ahead).
7. **Monte Carlo error.** Per card: se(p_path) = sqrt(p (1 - p) / n) (at most 0.0112), a Wilson 95 % interval, se(EV) =
   sd / sqrt(n), and the se of each exit-reason share. Per window: mean and max se, and the Brier inflation from
   simulation noise, mean p (1 - p) / n. This is simulation error only: sigma and beta are taken as known.
8. **Exit reasons.** The engine's reasons grouped as target (profit_take), stop, time (dte, before_expiry_day) and
   expiry (expiry, expiry_unsettled). The registered exit-reason score: the multinomial log score (eps 1e-6) of
   `p_path`'s shares against trailing Dirichlet(1) frequencies over these four categories of earlier resolved events
   (resolved strictly before the origin), same underlying and structure. Descriptive: no test is registered on it.
9. **Race.** Challengers = addendum A4's + `p_path`; incumbent `p_rw`, scores, tests and the gate unchanged. The
   registered Holm family (every non-incumbent forecaster x underlying) grows by `p_path`.
10. **Windows.** Replay windows only (design, seen, tuesday), none of which can adopt. The live card's PoP is unchanged:
    adoption needs forward shadow logging or the vault, each registered first.
11. **Later.** Marks on the P3.3 smile, paths from the P3.4 generator, bootstrap intervals over sigma and beta, and the
    live card's session calendar (`fincalc.bustime.nse_calendar`) are later addenda, registered before they are read.

# Pre-registration: same-print against next-print fills (P2.7(a), #382)

Registered 22-Oct-2026, in the PR that adds the variant to the engine and before any run of it on real data. No
next-print figure exists for the stored 2022-2026 history or the vault. The registered library (`backtest/specs.py`,
hash `6d92407630d7…` in `backtest/prereg.py`), `fno_backtest/PREREG.md` and the vault pack are not edited: this is a
registered SENSITIVITY of the library, not a new candidate, and it does not bump `VARIANTS_TRIED` (N stays 8, as
`vault/PREREG.md` §5 already states for `library_v1_next_print`).

## 1. The variant (code: `backtest.engine.run(fill=...)`, `backtest.compare`)

Every decision is the registered engine's, at the close of day t with day-t data only: which expiry, which strikes
(delta or % out of the money), the IV filter, the lot count (sized on day t's same-print fill prices, the registered
max-loss budget and the margin cap) and the management rules. Only the execution moves:

- **`next_open`** (primary): the order fills at session t+1. A leg that traded on t+1 fills at its t+1 OPEN; a leg
  that did not trade fills at its t+1 reference price (NSE's settlement price, the widest tier). This is the issue's
  "fill at the t+1 open (traded legs) or the t+1 close".
- **`next_close`** (secondary): every leg fills at its t+1 reference price (the last trade, else NSE's settle).
- Slippage: the registered tiers (`specs.Book.slippage_tiers`, minimum Rs 0.10), picked by the contracts traded in
  the FILL session (0 for an untraded leg); charges (`fincalc.charges`) on the fill session's date.
- At the fill, premium, defined max loss, entry costs, leg IVs and the approximate margin are recomputed at the fill
  prices; the IV reading that passed the filter stays the decision's. A credit spread whose fill gives no credit is
  refused ("no credit after slippage at the fill session"); a leg with no row on t+1 refuses the entry ("no price for
  a leg at the fill session"); an order decided on the last stored session never fills. Every refusal is counted.
- Exits lag the same way: a management rule (profit-take, stop, days-to-expiry, last session before expiry day) that
  fires at the close of day d leaves an exit order that fills at session d+1 by the same rule; a leg with no row then
  closes at its last mark with the widest tier. The last-session rule looks at the session after the fill, so a
  seller is out by the last session before expiry day, as registered; the entry guard looks the same session ahead.
  Expiry-day settlement (P0.2's final settlement price, exercise STT) is unchanged.
- A position filled at an open may be managed at that session's close; one filled at a close is first managed at the
  next close (as same-print positions are).

## 2. Hypothesis and tests (run once, on the out-of-sample window 1-Jul-2024 to the latest stored day)

Both runs replay continuously from the in-sample start, as `evaluate.evaluate` does; trades are assigned to the window
of their DECISION day. Pairs: a same-print trade and the next-print trade decided at the same close (`compare.paired`).

- **H1 (one-sided):** same-print fills overstate rupees per trade for the IV-filtered sellers (`nifty_pcs_m`,
  `nifty_ccs_m`, `banknifty_ic_m`, `nifty_ic_w`). Statistic: D = net(same) − net(next_open) per pair; the
  expiry-clustered t of mean D (`metrics.clustered_t`), clusters = the same-print trade's expiry; supported for a spec
  when t >= 1.645 (one-sided 5 %). Reported per spec and pooled over the four sellers (clusters = (underlying, expiry)).
- **H1b (the IV-regime idea, descriptive):** the same D restricted to pairs whose entry IV percentile is in the top
  third (the post hoc split of `fno_backtest/RESULTS.md`). There is no registered IV-regime spec; if one is registered
  later, this comparison is run on it by these rules before it is judged.
- Reported for every spec with data (8 registered; the overlays have no stock options stored): trades and pairs, Rs
  per trade under each fill, the change in expectancy, the stop count under each fill, mean D, t, one-sided p, and the
  pair counts that could not be matched.
- **Label:** a spec whose out-of-sample expectancy is > 0 under one fill and <= 0 under the other is
  "execution-sensitive" on its card; an expectancy that cannot be computed leaves the label unknown (never "not
  sensitive").
- The stress replay (slippage x 2) is run under each fill and reported beside them.
- A next-print report is never a candidate (`evaluate` gives it the status `sensitivity`) and is never read as the
  cards' evidence (`backtest.store.latest_run` serves same-print runs only).

## 3. Rule from now on

Any future candidate (a new registered spec or variant, or a Strategy Lab version promoted to real money) must ALSO
pass every registered criterion with `next_open` fills. The library's existing verdicts (0 of 8 candidates) are not
re-judged.

## 4. Not in this registration (later slices of #382)

The Phase B fills on the 15:15 snapshot (needs #363's recorded quotes), the stop-touch test at the front future's
intraday high and low (b), direction targets rescored from the next open (c), the measured cost model as a cost
variant (d, after #363's adoption test), daily re-marked margin (e). Each is registered before its own run.

# Pre-registration: the F&O card ledger (P2.1, #376)

Registered 22-Oct-2026, before any card forecast was logged to the forecast ledger or resolved. The append-only card
log (`card_log`, #360) has held the cards the owner was shown since #360 merged; none of their outcomes has been
looked at. Cards logged before this date are recorded and resolved by the same rules (`finresearch forecasts cards`
and the monitor's hourly forecast step), and are reported separately from those logged after it, so the reader can see whether they differ.

## Event

For each card with legs: **the P&L of one set (the card's base lot ratio) after costs is > 0 under the card's exit
plan, marked at each session's reference price in the stored NSE F&O bhavcopy.**

- Entry: the card's planned entry prices (the bid/ask mid when the card had live bid/ask, else the last traded price,
  as the card's `entry.levels_priced_at` says), at the IST date the card was logged; entry costs by
  `fincalc.charges` on that date with the backtest book's brokerage per order.
- Management: `backtest.engine.manage`, unchanged, from the first stored session after the entry date: profit-take
  and stop at the card's `profit_pct` / `stop_pct` of the entry net premium, the days-to-expiry exit, the last session
  before expiry day, or settlement on expiry day (P0.2's final settlement price, exercise STT on in-the-money longs)
  for a hold-to-expiry plan. Marks are the engine's reference price: the last trade when the contract traded that
  day, else NSE's settlement price. Exits pay the engine's slippage tiers and `fincalc.charges`.
- The exit plan is re-derived from the card's `exits.source` (the pre-registered recipe's own exits, or the rule of
  thumb for the card's structure and days to expiry) and must agree with the card's `profit_pct` and `stop_pct`;
  otherwise it is unknown and the forecast is void, never guessed.
- Outcome 1 if the net P&L per set > 0, else 0; the net P&L per set (Rs) is kept as the resolution value.
- Void (unscored, counted): a leg with no stored bhavcopy row on a session before expiry day that the set is marked
  on, a future leg (the engine marks options only), an unknown exit plan or lot size. Waiting cards (no legs) and
  strategy checks (no exit plan) are not forecasts.

## Probabilities

- The card's real-world PoP (method `rule-based F&O trade-plan card`, the call shown first on the card).
- The card's risk-neutral PoP (method `fnobook.card_forecasts.RN_METHOD`), the option market's own probability: scored in its own row, never in the headline.
- A card whose PoP is None is logged with probability None (coverage only, never scored as 0.5).
- Both PoPs are of "P&L after costs > 0 at expiry, held to expiry", not of the exit-plan event above. Scoring them
  against the exit-plan event is the point: the owner acts on the card's PoP under its exits.

## Independence rule

One scored event per (underlying, expiry, structure kind): the FIRST card logged in that cluster that carries a
probability and resolved (not void). Later cards on the same cluster are kept and resolved, never scored.

## Primary statistics (two)

1. **PoP Brier skill against the risk-neutral PoP**: BSS = 1 - Brier(real-world) / Brier(risk-neutral) on the paired
   independent events (both probabilities present).
2. **Cohort delta**: mean net P&L per set of the ENTER or NO PROVEN EDGE cards minus that of the WAIT or AVOID cards,
   with an expiry-clustered t (`backtest.metrics.clustered_t`).

Secondary (reported, never gated): log score; reliability bins with Wilson intervals; coverage of the logged P&L
quantiles (share of outcomes below the logged p10 against 10 %, binomial test); override rate and override P&L once
the override log exists.

## Claims

No calibration claim before >= 100 non-overlapping forecasts per symbol x event. Expected volume is about 70-110
independent card events a year: enough for calibration-in-the-large within a year, not for a significant cohort
effect. Nothing here changes a card's verdict.

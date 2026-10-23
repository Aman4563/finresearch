# Addendum to the card ledger registration (P2.1, #376)

`PREREG.md` (22-Oct-2026) is unchanged. Each entry below is dated and committed before any code that reads an
outcome it concerns. Entries are post hoc relative to PREREG.md only where they say so.

## B1. 23-Oct-2026: the scoring report, the override log, the fixed probability panel

Recorded before the report, the override log or the panel was built, and before any card or panel outcome was read
(no card forecast in the live ledger had been looked at; the panel had never run).

### B1.1 The scoring report (`fnobook/card_report.py`)

- **Events** are `horserace.ledger_events.events_from_rows` over the ledger's resolved card forecasts, unchanged:
  each card's real-world and risk-neutral rows paired; `first` = the PREREG's independence rule (the first card per
  underlying, expiry and structure that carries a real-world probability and resolved). Every statistic below is on
  the `first` events unless it says otherwise. Void and open cards are counted, never scored.
- **Split** (PREREG: "reported separately"): `before` = cards whose IST logging date (`origin`) is before
  2026-10-22, `after` = on or after it. Both halves are reported side by side and as a pooled column; the pooled
  column is never the headline.
- **Primary 1, BSS** = 1 - Brier(real-world) / Brier(risk-neutral) on the `first` events whose risk-neutral row
  also carries a probability (`skillmon.stats.bss`), with its moving-block bootstrap interval (`skillmon.stats.bss_ci`,
  h = 1, 2,000 resamples, seed 376). None (unknown) with no paired event or a zero benchmark loss.
- **Primary 2, the cohort delta** = mean net P&L per set of ENTER or NO PROVEN EDGE cards minus that of WAIT or
  AVOID cards (`ledger_events`' gate class; other verdicts are unknown and left out, counted). PREREG names
  `backtest.metrics.clustered_t`, which is a one-sample t of a mean; a difference of two group means needs the
  regression form. Implementation (statistic unchanged): `horserace.scores.gate_information`, i.e. P&L = a + delta
  1{pass} by OLS with the CR1 expiry-clustered variance and a t(G - 1) two-sided p. Disclosed as an implementation
  choice the PREREG left open.
- **Secondary** (reported, never gated): log score (clip 1e-6) and Brier of each model; reliability bins with Wilson
  intervals (`evals.calibration.reliability_bins`, 5 bins); **PIT** (added here; PREREG did not list it for cards):
  `horserace.scores.pit` of the realised P&L per set under the card's 20 logged P&L dots, as a 10-bin histogram,
  with the clipped count; **p10 coverage**: the share of events whose P&L per set is below q(0.10) of the logged dots
  (`horserace.scores.quantile_fn`, piecewise-linear through the dots at (i + 0.5) / 20), against 10 % by the exact
  two-sided binomial test (`horserace.scores.binom_two_sided`). Cards without dots are counted, not scored.
- **Claims gate**: per underlying, `n_independent` and `claim_allowed = n_independent >= 100`; below it every number
  is labelled descriptive and the page says no calibration claim is made.

### B1.2 The override log (`card_override`, personal)

- One row per decision the owner records with POST `/api/fno/overrides`: (card_log id, card index, decision
  `traded` | `skipped`, optional note, the card's verdict class copied from `card_log` at write time). Append-only (a
  trigger refuses UPDATE and DELETE). Never inferred from the paper book or broker positions: an unrecorded card has
  no decision, never "skipped".
- When a card has several decisions, the latest counts; every row is kept.
- **Override** = `traded` on a WAIT, AVOID or NO PROVEN EDGE card, or `skipped` on an ENTER card (the issue's
  wording; NO PROVEN EDGE sits with ENTER in the cohort delta but counts as a "don't" here). A card whose verdict
  class is unknown is counted and left out.
- **Override rate** = overrides / recorded decisions with a known class.
- **Override P&L vs card-following P&L**: the card ledger's simulated net P&L per set (market data, never the
  owner's own fills) averaged in four groups: traded against the card, traded with it, skipped an ENTER card,
  skipped a non-ENTER card; with n and the number still open or void. Every resolved card counts here (decisions are
  per card, not per independent event); the count of distinct clusters is shown beside it.

### B1.3 The fixed probability panel (`signals/prob_panel.py`, table `prob_panel`, market data)

Logged for every stored NSE F&O bhavcopy session on or after 2026-10-23, so the owner's choices cannot bias the
sample. Forward only (the 2022-26 replay and the vault cover history). Recorded by the monitor's hourly forecast step
and `finresearch forecasts panel`; idempotent on (session, underlying, expiry, event).

- **Underlyings**: NIFTY, BANKNIFTY. **Expiries**: near = the earliest listed expiry after the session; monthly =
  the earliest listed expiry after the session with a future trading (`Day.is_monthly`). When they coincide, one set
  of rows (kind `near, monthly`).
- **Inputs at the session close** (horserace addendum A1.7, A1.8 and `backtest.pricing`): F = `pricing.forward`,
  T = calendar days / 365, sigma_ATM = `horserace.replay.atm_iv`, RV20 and RV60 = `horserace.replay.rv` on
  same-instrument returns (`replay.same_instrument_return`) over the stored sessions up to the close.
- **Strikes** (fixed at the close, never snapped to a listed strike): the 10-, 16- and 25-delta put and call strikes
  by Black-76 forward delta (undiscounted) at sigma_ATM: call K = F exp(-N^-1(d) s + s^2/2), put
  K = F exp(-N^-1(1 - d) s + s^2/2), s = sigma_ATM sqrt(T). The +/-1 sigma band: [F e^-s, F e^s].
- **Events**: `put10/16/25` = 1{S_T < K}; `call10/16/25` = 1{S_T > K}; `band1s` = 1{F e^-s <= S_T <= F e^s}.
  S_T = the expiry's final settlement price (`backtest.pricing.settlement_price` on the contract's last stored
  session, P0.2's rule). Unknown settlement: void with the reason.
- **Models** (each a lognormal around F, the drift-r case of the card): `rn_atm` (sigma_ATM), `rw20` (RV20), `rw60`
  (RV60). P(S_T > K) = N(d2), d2 = (ln(F/K) - sigma^2 T / 2) / (sigma sqrt T). An unknown sigma is an unknown
  probability (None), never 0.5.
- **Recipes**: each registered replay spec (`horserace.specs.REPLAY.specs`) whose entry window holds the session, as
  `horserace.replay.card_at` builds it (its own expiry and legs; addendum A1); logged: PoP under `p_rw20`, `p_rw60`,
  `p_rw` and `p_rn_atm`. Event and resolution: the card ledger's (`fnobook.card_forecasts.simulate`).
- **Terminal z / PIT** (tail and band rows): z = [ln(S_T / F) + sigma^2 T / 2] / (sigma sqrt T) per model, PIT =
  N(z), a 10-bin histogram per model and expiry kind.
- **Independence**: one scored event per (underlying, expiry, event): the first session logged. Brier and log
  score per model, event and underlying on those events; `claim_allowed` at >= 100 such events, as B1.1.
- **Why its own table**: about 50 rows a session would otherwise enter the forecast ledger's headline scan and the
  skill monitor's cells; the panel is scored only on its own page.

### B1.4 Recording at POST time

POST `/api/fno/plan` records the card's two forecasts in the same request, right after the card_log row is written
(the same rules as `record_pending`); a failure is logged and the hourly step records it later. The suggest GET is
unchanged (still recorded by the hourly step: GETs never write forecasts).

## B2. 23-Oct-2026: two panel resolution rules decided while building it (post hoc relative to B1)

Recorded before the panel ever ran on stored data or read an outcome; B1 is unchanged.

1. **Which session settles.** B1.3 says S_T is `backtest.pricing.settlement_price` on the contract's last stored
   session. When expiry day itself is stored, that session is used. When it is not (a holiday, or a missed download),
   the last stored session before it is accepted only if its expiring options carry ONE uniform settlement price, which
   NSE prints only on the contract's last session; otherwise the event is void ("not provably the contract's last
   session"), never settled at an earlier session's price.
2. **When a row is tried.** No panel row is tried before its expiry date (IST), like the card ledger's
   `resolve_on` = expiry; a recipe whose stop fires early is resolved at its expiry, as the cards are.

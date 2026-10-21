# Pre-registration: the card horse race (P2.2, #377), the adoption gate for P3

Registered 22-Oct-2026, before any horse-race score was computed on a real outcome. The registration is
`src/finresearch/horserace/specs.py` (`card-horse-race-v1`); its sha256 `11f10c09f6a629ec09b3115a37008afaf0f2917b69cb56abd716157be4701060`
is pinned in `horserace/prereg.py`, and the harness refuses to score while the two differ. This file restates it in
prose; where they differ, `specs.py` wins. It is never edited: changes go to `ADDENDUM.md` as post hoc, with a new
`SPEC_VERSION` and hash committed before the changed race reads an out-of-sample outcome.

The harness searches no new strategies and adds nothing to `VARIANTS_TRIED` (backtest or direction).

## Event

For each card: **1{P&L of one set after statutory costs and the engine's tiered slippage > 0 under the structure's
exit plan}**, with the Rs P&L per set kept. For logged cards this is exactly the card ledger's event
(`evals/experiments/card_ledger/PREREG.md`, resolved by `fnobook.card_forecasts`); a void card is not an event.

## Forecasters (every one is reported, whatever the outcome)

| key | forecast | dots + EV |
| --- | --- | --- |
| `p_rw` (incumbent) | the card's headline real-world PoP: the lower-EV of `p_rw20` and `p_rw60` | yes |
| `p_rw20`, `p_rw60` | lognormal at the 20- / 60-day realised volatility, drift r (replay only: a logged card keeps only the headline) | yes |
| `p_rn_atm` | lognormal at the ATM IV, drift r: the option market's own probability (benchmark) | yes |
| `p_base` | trailing base rate (k + 1) / (n + 2) of earlier resolved events, same underlying and structure | no |
| `p_gate` | the same rate within the card's gate class (pass = ENTER or NO PROVEN EDGE, fail = WAIT or AVOID) | no |

"Earlier" = resolved strictly before the forecast origin. Dots = the card's 20 P&L quantiles at (i + 0.5) / 20. P3
challengers (IV-scaled empirical, EWMA/GJR-FHS, SVI/Breeden-Litzenberger) are added to `CHALLENGERS` one at a time,
each under a new hash committed before its out-of-sample score is read. Only a forecaster with dots can be adopted.

## Scores

- **Primary: Brier** of each PoP. Secondary: log score (p clipped to [1e-6, 1 - 1e-6]); Murphy decomposition
  BS = REL - RES + UNC on 10 equal-width bins, with the within-bin residual reported (the identity is exact only when
  each bin holds one forecast value); reliability bins with Wilson intervals.
- **Distribution (secondary):** the quantile function is piecewise-linear through ((i + 0.5) / 20, dot_i), its end
  segments extended to u = 0 and 1. CRPS(F, y) = 2 ∫ (1{y < q(u)} - u)(q(u) - y) du, midpoint rule on 2,000 nodes.
  PIT u = F(y), clipped to [0.001, 0.999] (clips counted).
- **Tails at alpha = 0.10:** VaR = q(0.10) (between dots 2 and 3); ES = (1/alpha) ∫_0^alpha q(u) du. Kupiec POF
  (LR, chi-square 1 df); Christoffersen independence (LR, 1 df); Berkowitz LR (3 df) on z = Φ⁻¹(u) against AR(1)
  Gaussian with conditional likelihood; Acerbi-Szekely Z2 = 1 - Σ X_t 1{X_t < VaR_t} / (T alpha ES_t) with ES_t the
  signed tail mean (Z2 < 0: tail risk under-forecast), its p-value from 10,000 simulations of each event's own F
  (seed 377); events whose tail mean is >= 0 are excluded from Z2 and counted. 5 % levels are not resolvable from 20
  dots and are not tested.
- **EV calibration:** PnL_i = a + b EV_i, OLS, CR1 expiry-clustered SE, t with G - 1 df; calibrated iff the 95 % CI
  of b contains 1 and that of a contains 0.
- **Gate information:** PnL_i = a + delta 1{gate passes}, expiry-clustered, two-sided.
- Exit-reason probabilities (multinomial log score against trailing Dirichlet(1) frequencies) and drawdown bands
  (share inside against nominal cover, binomial) are scored for forecasters that produce them; none does today.

## Tests and the adoption gate

- DM with the HLN correction on per-(underlying, expiry) mean score differentials, one-sided (challenger better),
  h = 1, Newey-West lag 1, at least 10 expiries; Holm across every non-incumbent forecaster x underlying, per window
  and score.
- Tail tests run as tests only on >= 100 independent events: one per (underlying, expiry, structure), the first
  entry session. Smaller samples (the 27 monthly expiries) are reported descriptively.
- **Adoption:** a challenger replaces the incumbent for an underlying only if, in a primary window, it beats it on
  Brier (DM-HLN, Holm p < 0.05) AND, on >= 100 independent events, Kupiec at 0.10 has p >= 0.05 AND Berkowitz has
  p >= 0.05. A smile model is not called more accurate unless the Brier condition holds. Nothing here changes a
  card's verdict.

## Windows and power

- **Primary:** forward card-ledger events logged on or after 22-Oct-2026; and the vault (2008-2021), registered
  separately in `evals/experiments/fno_pop_calibration/PREREG.md` (one read, not run by this harness).
- **Replay of 2022-04-01 to the last settled expiry:** the six registered index specs on their own underlying, one
  card per stored session inside the spec's entry window, legs by the registered delta rule at the close (anchor
  >= 10 contracts), entry at the settlement price, exits by `backtest.engine.manage`, forecasts from data stored on or
  before the origin. 2022-04-01 to 2024-06-30 is for design choices only; 2024-07-01 on is secondary and labelled
  seen; the post-2-Sep-2025 Tuesday-expiry regime is a separate secondary run. None of these can adopt a model alone.
- **Power, stated up front:** about 115 NIFTY weekly, 27 NIFTY monthly and 27 BANKNIFTY monthly expiries in 2024-26;
  about 70-110 independent card-ledger events a year. The monthly samples cannot pass the tail gate; adoption on
  forward data needs >= 100 independent events per underlying, so it is at least a year away.

## Expected result, stated before the read

The issue's measurement (terminal moves beyond 2 sigma at RV20 on 7.4 % of NIFTY and 13.3 % of BANKNIFTY near
expiries against the model's 4.6 %) suggests the RV lognormal overstates sellers' PoP and EV. That is a prior, not a
claim.

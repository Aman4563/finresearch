# Addendum: inference recalibration (issue #381, P2.6), POST HOC to PREREG.md

Written 22-Oct-2026, after the first real-data results (RESULTS.md) had been seen. Nothing here edits `PREREG.md`,
`backtest/specs.py` or its registered hash (`6d92407630d7…`, unchanged). Every item is labelled with the direction
in which it moves a verdict. **No published verdict changes**: the registered criteria still decide every status, and
criteria v2 below is a registered SHADOW that is reported beside them and never sets a status.

## 1. Registered engine fix: a zero daily Sharpe enters the trials' variance (post hoc, v1)

`evaluate._finish` collected the trials' out-of-sample daily Sharpe ratios with a truthiness test, so a daily SR of
exactly 0 was dropped from V (the variance of the trials' SRs in the deflated Sharpe). Now only an unknown SR (None)
is left out (`tests/test_backtest_inference.py::test_a_zero_daily_sharpe_enters_the_trials_variance`).
Direction: either way (adding a 0 can raise or lower V). Effect on the published run: none, because no published
strategy had a daily SR of exactly 0 (Sharpes −1.00, 0.24, 0.10, 0.44, −2.62, 1.07). The Strategy Lab's own SR
list (`stratlab/store.trial_srs`) already used `is not None`.

## 2. Criteria v2: a new registered criteria variant, shadow only, NOT adopted

`src/finresearch/backtest/criteria_v2.py`, hash `afc521c16cde…` recorded in `backtest/prereg_v2.py` (the hash covers
v1's registered hash, so either changing is detected; `tests/test_backtest_criteria_v2.py`). `adopted = False`.
Adoption needs, together: #380 (P2.5: sealed holdout, every trial logged), the DSR-rule simulation (a) on the real
out-of-sample seller P&L, and an owner decision. Until then each judged strategy carries `criteria_v2` with
`status: shadow_not_adopted` and `verdict: would_pass | would_fail`, shown on /fno/backtests under its status.

| item | v1 (registered) | v2 (shadow) | direction against v1 |
|---|---|---|---|
| P&L judged | net after costs | net after costs **less the yield the cash half of the margin could earn** (6.5 % a year [U], cash share 50 %) | stricter |
| t-statistic | expiry-clustered | two-way clustered, entry month × expiry (Cameron, Gelbach & Miller 2011, eq. 2.15; negative V → larger one-way, their p. 9) | not monotone; equal when each expiry has one entry month (the monthly specs, nearly) |
| CI | i.i.d. percentile bootstrap of expiries | BCa (Efron & Tibshirani 1993, eqs. 14.10, 14.14, 14.15) on a stationary bootstrap (Politis & Romano 1994) of expiries in time order, mean block ⌈G^(1/3)⌉ | not monotone |
| max drawdown | % of starting capital | % of the running peak | **more permissive** whenever the peak is above the capital |
| deflated Sharpe | N = VARIANTS_TRIED = 8, V = sample variance of the 6 known SRs | N = the trials whose SR enters V (6); V = `dsr_rule` = max(V_null, V_cand) | N alone is **more permissive**; max(·) only raises V |
| reported, not judged | — | Lo (2002) annual Sharpe (eq. 20/A18, ρ̂_k beyond the Newey-West lag set to 0, our truncation); posterior Sharpe under N(0, 0.5²) | — |

**Decision needed: the issue contradicts itself on V.** Its Maths line says "register max(V_null, V_cand)", but the
bars it quotes ("about 2.0-2.2 annualised at T = 570; 0.85 at T = 3,400 with N = 8; about 1.0 with N = 30") are those
of V_null alone (reproduced: 2.07, 0.85, 1.01 in `test_v_null_bars_quoted_in_the_issue`). On the published run
V_cand = 0.00694 > V_null = 1/569 = 0.00176, so max() = V_cand and the bar at T = 570 is 2.82 annualised with N = 6
(3.03 with v1's N = 8), not 2.0-2.2. v2 registers `max` as the issue's Maths line says; acceptance (a) compares
v_null, v_cand and max as separate rules, and its result (section 4) is what should settle it, on real P&L.

Published Sharpes under each rule (T = 570, Normal moments assumed because skew and kurtosis are not in RESULTS.md):
the largest DSR is the debit put spread control's 0.62 under V_null (0.16 under V_cand or max; 0.10 under v1).
Every value is far below 0.95, and the sellers fail on t and on the CI independently, so no v2 shadow verdict on the
published numbers can read "would pass".

## 3. Opportunity cost (shown, never a criterion)

Each strategy with an out-of-sample window shows its net P&L after the owner's slab rate plus 4 % cess (F&O is
non-speculative business income; a net loss is shown untaxed: set-off is not modelled) beside holding NIFTY on the
same ₹ capital over the same days, after listed-equity capital-gains tax from `fincalc.tax.classify` (12.5 % LTCG /
20 % STCG from 23-Jul-2024). The index leg is the APPROXIMATE total-return index from the stored daily closes and
dividend yield (`refseries.index_eod`, `fincalc.refseries.approx_tri`); NSE's own TRI is not stored yet (P1.4).
No index rows covering the window (within 7 days of each end) or an unreadable profile reads "unknown", never ₹0.
The ₹1.25 lakh LTCG exemption and surcharge are not applied. Computed on read, so no saved run stores a personal
figure.

## 4. Validation simulations (`finresearch fno backtest recal-sim`, `backtest/recal_sim.py`)

RESULTS_PLACEHOLDER

## 5. Not changed, on purpose

- The registered DM tests (`direction/evaluate.py`, `horserace/race.py`) still call the Bartlett + HLN version; the
  fixed test is `direction.metrics.diebold_mariano_hln`. Switching either registered consumer is a post hoc change
  of its own experiment (a decision; #381 reports every published direction-model Holm p ≥ 0.66, so the over-size
  changed no published verdict).
- `evals/stock_universe.py` keeps its own registered DSR copy.
- The Strategy Lab judges with v1 (P2.5 territory); the v2 functions are importable for it.

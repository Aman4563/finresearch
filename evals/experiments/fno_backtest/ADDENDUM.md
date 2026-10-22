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

Run 22-Oct-2026: `finresearch fno backtest recal-sim --json …` with the defaults (seed 381; (a) 2,000 programmes
per cell, 500 bootstrap draws; (b) and (c) 2,000 replications); 571 s. Full output:
`recal_sim_2026-10-22.json` beside this file. Seeded, so the figures reproduce exactly.

**(c) DM size at nominal 5 %** (d = equal-weight MA(h−1) of N(0, 1) shocks, one-sided; our own Monte Carlo, since the
review's `critic2/checks.py` is not in this repository):

| n | h | registered (Bartlett, max(h−1, 5) lags, HLN) | fixed (rectangular, h−1 lags, HLN) |
|---|---|---|---|
| 200 | 5 / 10 / 22 | 8.3 % / 9.5 % / 10.2 % | 5.7 % / 6.6 % / 7.9 % |
| 570 | 5 / 10 / 22 | 8.7 % / 8.9 % / 9.7 % | 5.7 % / 4.9 % / 5.8 % |

The registered test over-rejects as the issue said (8-10 %); the fixed one is near nominal at n = 570 and still
over-sized at n = 200 with h = 22 (7.9 %). Its variance was never non-positive in these runs (0 of 12,000).

**(b) 95 % CI coverage of a known mean** (target 93-97 %):

| n | DGP | i.i.d. percentile | stationary percentile | stationary BCa | Newey-West t |
|---|---|---|---|---|---|
| 60 | iid / AR(1) 0.3 / GARCH-t / AR(1)+GARCH-t | 94.0 / 81.9 / 94.5 / 82.5 | 90.5 / 86.8 / 91.7 / 88.3 | 90.2 / 85.6 / 90.6 / 85.9 | 92.0 / 88.5 / 93.0 / 90.2 |
| 570 | the same | 94.5 / 84.2 / 94.5 / 83.8 | 93.4 / 92.4 / 94.0 / 92.7 | 93.5 / 91.9 / 93.3 / 91.8 | 94.1 / 92.6 / 94.8 / 93.5 |

No method lies in 93-97 % on every DGP. At n = 60 (the scale of 30-113 trades per spec) none is adequate under
serial dependence. At n = 570 the Newey-West t-interval is closest (3 of 4 DGPs in range, AR(1) 92.6 %), then the
stationary percentile; the stationary BCa that criteria v2 registers covers 91.8-93.5 % and does NOT meet the
acceptance bar. **Decision needed** before any adoption: keep BCa (as the issue specifies), switch v2's CI to the HAC
t-interval or the stationary percentile (a new v2 variant, re-registered), or accept that no interval is honest at
this trade count.

**(a) DSR rule, FWER and power — on a SYNTHETIC seller-like base only** (570 days, 4 columns: N(0.05 %, 0.4 %) days
with 2 % crash days of −Exp(2 %); ρ = 0.5). `finresearch_fnohist` is not on this machine and the live database was not
read, so the real-P&L run (`--history-url postgresql+psycopg://localhost/finresearch_fnohist`) is still to do; these
figures do not decide anything.

| N | rule | FWER at SR 0 | FWER range over SR 0-1.5 | power at SR 0.5 / 1.0 / 1.5 |
|---|---|---|---|---|
| 8 | DSR V_null | 3.8 % | 2.2-3.8 % | 2.3 / 6.6 / 17.3 % |
| 8 | DSR V_cand (= v1 here) | 0.4 % | 0.2-0.4 % | 0.4 / 2.0 / 3.8 % |
| 8 | DSR max(V_null, V_cand) | 0.4 % | 0.2-0.4 % | 0.4 / 1.9 / 3.8 % |
| 8 | Romano-Wolf | 7.1 % | 6.3-7.1 % | 4.8 / 13.4 / 30.6 % |
| 8 | SPA_c | 7.1 % | 4.0-7.1 % | 4.5 / 12.9 / 29.7 % |
| 30 | DSR V_null | 3.0 % | 2.5-3.0 % | 1.0 / 3.8 / 8.9 % |
| 30 | DSR V_cand / max | 0.9 % | 0.5-0.9 % | 0.3 / 1.5 / 3.8 % |
| 30 | Romano-Wolf | 6.3 % | 5.9-6.3 % | 2.4 / 6.3 / 16.1 % |
| 30 | SPA_c | 6.2 % | 4.5-6.2 % | 2.1 / 6.1 / 15.7 % |

Read with the acceptance rule ("FWER ≤ 5 % and the highest power at SR 1.0"): on this synthetic base it picks DSR with
V_null, not the max(V_null, V_cand) that v2 registers; Romano-Wolf and SPA (studentised by each resample's own sd,
no HAC) exceed 5 % FWER. max(·) and V_cand are very conservative (FWER < 1 %). Power at a true annual Sharpe of 1.0
is 2-13 % for every rule: as the issue says, no rule rescues power at T ≈ 570. If the real-P&L run agrees, v2's
`dsr_rule` should become `v_null` as a new, re-registered v2 variant (more permissive than v1 and than the
registered v2: the bar falls from ~3.0 to ~2.0 annualised at T = 570). That is an owner decision, after #380.

## 5. Not changed, on purpose

- The registered DM tests (`direction/evaluate.py`, `horserace/race.py`) still call the Bartlett + HLN version; the
  fixed test is `direction.metrics.diebold_mariano_hln`. Switching either registered consumer is a post hoc change
  of its own experiment (a decision; #381 reports every published direction-model Holm p ≥ 0.66, so the over-size
  changed no published verdict).
- `evals/stock_universe.py` keeps its own registered DSR copy.
- The Strategy Lab judges with v1 (P2.5 territory); the v2 functions are importable for it.

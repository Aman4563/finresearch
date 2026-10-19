# Pre-registration: F&O direction model (issue #327)

Registered 19-Oct-2026, **before any evaluation on real data**. The read-only market-data copy
(`finresearch_fnohist2`) had been inspected only for its table list, row counts and date coverage (which columns are
filled in which years); no feature, target, forecast or metric had been computed on it. The registration is
`src/finresearch/direction/specs.py`; its SHA-256 (`specs.registration_hash()`) is recorded in
`src/finresearch/direction/prereg.py`, and the evaluator refuses an out-of-sample run whose hash differs. Nothing
below changes after the first real run; anything added later goes in RESULTS.md or an ADDENDUM.md, labelled post hoc.

## Question

Can a small, pre-specified model say CALL or PUT on NIFTY / BANKNIFTY better than (a) the trailing base rate and
(b) the probability the option market itself implies, out of sample, after accounting for overlapping outcomes and
for testing 24 cells? Prior: almost certainly not (Welch & Goyal 2008, RFS 21(4) [U]: return predictors that look
good in sample mostly fail out of sample). The deliverable is honest either way: if nothing passes, the app shows the
market-implied probabilities labelled "no edge" and says NO TRADE.

## Targets

For h ∈ {1, 5, 10, 20} sessions, R = ln(S[t+h]/S[t]), σ_h = IV30[t]·√(h/252):
`up` R > 0, `up_k` R > 0.5·σ_h, `down_k` R < −0.5·σ_h.
S: the bhavcopy underlying value (2024 on, the whole OOS window); before 2024 the front-month future's daily return
(same contract both days) stands in for the index return. A stored `index_eod` table, if present, replaces both.

## Features (at day t's close only)

5/20/60-session returns; distance to the 20/50/200-session average; ADX(14) on the front future (forward
ratio-adjusted at rolls); realised volatility 20/60; IV30 and its 252-session rank; IV30 − RV20; term slope
(next − near ATM IV); 25-delta skew; put-call OI ratio and its 5-session change; 5-session change of option OI and of
futures OI; FII and Pro net index futures and options positioning (only days of history: dropped by the
missing-data rule); days to the nearest expiry; expiry-day flag; day of week. Not available: historical RBI /
results / Budget calendar. Look-ahead guard: features are recomputed after every later row is replaced by junk
(`features.build_features(check_every=...)`), and training rows must have i + h ≤ the refit session.

## Models

(a) trailing base rate of the last 500 known outcomes (benchmark); (b) market-implied lognormal with σ = IV30, drift
r − σ²/2, r = 6.5 %, no dividend yield (benchmark); (c) logistic regression, L2 penalty α·h with α = 0.5 on z-scored
features (train-window statistics, clipped ±5, missing = 0, a feature missing on > 50 % of training rows dropped),
refitted on the first session of each month on rows whose outcome was known. α was chosen on synthetic panels only
(a null random walk and a planted put-call-ratio edge), not on real data. No gradient boosting (no library in the
dependencies). Windows: train from 1-Jan-2022, in-sample to 30-Jun-2024, out of sample from 1-Jul-2024 to the last
session whose outcome is known.

## Acceptance (a logistic cell is "accepted" only if all hold out of sample)

1. ≥ 250 OOS days.
2. Brier skill > 0 against the implied model AND against the base rate.
3. Each of those two Diebold-Mariano tests (Brier differential; Newey-West lag max(h − 1, 5); Harvey-Leybourne-Newbold
   correction; one-sided Student-t) has a Holm-adjusted p < 0.05 across the 24 logistic cells (2 underlyings × 4
   horizons × 3 events; one family per comparison).
4. Calibration slope (o ~ a + b·logit p) between 0.5 and 3.0 (below 0.5 is too extreme to price trades with; above 1
   is timid, which only understates an edge; the penalty makes planted edges timid, slope 1.5-2.7 on synthetic data).

Primary cell, read first: NIFTY, h = 5, `up`. Why both benchmarks: the variance risk premium lets a constant beat the
implied model on the ±0.5σ events with no directional information, so beating the implied model alone proves nothing
about direction.

Power, stated before the real run (synthetic panels, ~600 OOS days): a planted daily correlation of 0.2 between the
signal and the next return (far larger than anything documented for index returns) gave Brier skill ≈ +0.01 at h = 1
and did not pass the Holm-adjusted tests; 0.3 passed the tests. So this design can only detect large edges; a "no edge"
result means "no edge large enough to measure with ~2 years of OOS data", not proof of zero.

## Decision layer

Model probabilities at horizon h become a distribution of S[t+h] by reweighting the implied lognormal's regions
(R < −kσ, −kσ..0, 0..kσ, > kσ) to the model's masses. Candidate trades on the live chain (long call, long put, bull
call spread, bear put spread, bull put credit spread, bear call credit spread; strikes at the money and 1 % of spot
away) are valued at t + h (Black-Scholes at each leg's IV for any time left), EV = Σ probability × payoff − costs
(entry plus square-off charges, fincalc.charges). A trade is recommended only when its cell is accepted, EV_model >
EV_implied, and EV_model after costs ≥ max(5 % of the max loss, the costs). Otherwise: NO TRADE, no measured edge.
Live forecasts are logged daily to the forecast ledger (the shown method, plus the logistic as a shadow when it is not
accepted); promotion on live evidence is the owner's decision (specs.LIVE_PROMOTION), never automatic.

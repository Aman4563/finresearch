# Pre-registration: E-IPO-1, post-hoc calibration of the IPO listing model

Registered 1-Oct-2026, before the experiment code was run on the data. Issue #147 (part of #101).
Research basis: portfolio-insights research §2.1 (E-IPO-1), roadmap §D.1 and §C.10, Niculescu-Mizil & Caruana 2005 [31].

## What was already known (disclosed, so the reader can judge the risk of a forked path)

The committed walk-forward report `src/finresearch/evals/artefacts/ipo_model_walkforward.json` was seen before this
was written: the L2 logistic model has better discrimination than the base-rate table (pooled AUC 0.831 vs 0.810)
but a worse pooled Brier score (0.1560 vs 0.1529) and passes the Brier-skill gate in only 3 of 7 years (2019, 2020,
2023). Its lowest reliability bin forecasts 0.19 against an observed 0.39. Calibration is the obvious response to
that pattern, which is why it is tested; the test years themselves are never used to fit anything.

## Data

- `data/ipo_history.jsonl` as exported from the live database on 30-Sep-2026,
  sha256 `5abee9980957402262b2b3bec86b21b7d915a27852f05186ac574d4aa386bf6a` (461 rows; 423 usable mainboard EQ
  rows under `evals.ipo_model.usable`, listing years 2016–2026).
- Event: listing-day open > issue price. Nothing about the data, features or base model changes.

## Method

1. **Base model, unchanged:** `evals.ipo_model.fit` (L2 logistic, λ = 1, the nine committed features) and the
   base-rate table `evals.ipo_model.table_forecaster` (QIB band × regime, Laplace-smoothed, MIN_CELL fallbacks).
2. **Out-of-fold (OOF) forecasts:** for every listing year t with at least 20 training rows in years < t
   (so t = 2017…2026), fit on years < t and forecast year t. Each OOF record is (year, p_model, p_table, y).
3. **Calibration inside each gate fold:** for gate year t ∈ 2019…2025, the calibration pool is the OOF records with
   year < t (only forecasts that were themselves out of sample; nothing from year t or later). If the pool has
   fewer than 30 records, the variant falls back to the uncalibrated p_model for that year. (Expected pool sizes:
   2019 → 50, growing each year.)
4. **Variants (three, all pre-specified; no others will be tried):**
   - **T, temperature scaling:** p′ = σ(logit p / T), T minimising log loss on the pool, T ∈ [0.2, 5]
     (golden-section search). One parameter.
   - **P, Platt scaling:** p′ = σ(a·logit p + b), maximum likelihood on Platt's smoothed targets
     t₊ = (N₊+1)/(N₊+2), t₋ = 1/(N₋+2). Two parameters.
   - **S, shrinkage blend to the table:** p′ = λ·p_model + (1−λ)·p_table, λ ∈ {0, 0.05, …, 1} minimising the pool's
     Brier score (ties → smaller λ). One parameter. λ = 0 reproduces the table exactly.
   Implementations: `finresearch.evals.calibration_policy` (`fit_temperature`, `fit_platt`, `fit_blend`).

## Metrics

Per gate year: Brier of each variant, of the table and of climatology; BSS vs the table = 1 − BS_variant/BS_table;
AUC; log loss. Pooled over 2019–2025 (n = 288): Brier, BSS vs table, AUC, log loss, five equal-count reliability
bins. 2026 (partial year) is reported only, never gated. The fitted parameter (T, a/b, λ) per year is reported.

## Pass bar (per variant)

A variant **passes** only if BOTH:
1. Brier skill vs the base-rate table > 0 in **at least 5 of the 7** complete years 2019–2025 (the existing gate), and
2. the **pooled 2019–2025 Brier** is lower than the table's pooled 2019–2025 Brier.

Three variants are tested against the same bar; the bar is not corrected for that multiplicity, so a pass is
further required to survive the deployment check below before anything ships.

## Deployment check (live-feature parity)

The live signal (`signals/ipo.py::_live_features`) cannot yet supply two features: the OFS share is always
imputed (ofs_missing = 1, training median) and the Nifty 20-session return is always 0. A variant validated on
features the live path does not have must not ship. So a passing variant is re-run with those two features
blanked at PREDICTION time for every OOF and test forecast (training fits keep the real features, as the deployed
model would), with its calibrator fitted on those blanked OOF forecasts. It must pass the same two-part bar again.

## Decision rule (fixed now)

- If one or more variants pass both the bar and the deployment check: ship the first in the order **T, S, P**
  (fewest parameters and least model dependence first). `signals/ipo.py` then uses the model + that calibrator
  behind the existing gate, validation status "backtested", with the walk-forward numbers in the description; the
  deployed calibrator is refitted on all OOF forecasts 2017–2026, and the pooled reliability bins used for the
  signal's range are recomputed on the calibrated 2019–2025 OOF forecasts.
- If a variant passes the bar but fails the deployment check: record it; the signal stays on the base-rate table;
  the follow-up is to fill the two live features (#137 gap) and re-run this pre-registration unchanged.
- If none passes: record the numbers honestly and change nothing in `signals/`.

Artefacts: `results.json` and `RESULTS.md` in this directory, produced by
`uv run python -m finresearch.evals.ipo_calibration --data ../finresearch/data/ipo_history.jsonl`.

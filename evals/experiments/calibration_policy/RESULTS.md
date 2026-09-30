# Calibration policy: results

Implemented in `src/finresearch/evals/calibration_policy.py` exactly as pre-registered (tiers 50 / 100 / 1000 on
effective n; shrink k = 75; Platt on smoothed targets; PAV isotonic; overlap 12 for the stock 12-month event).
Tests: `tests/test_prediction_experiments.py` (all offline, seeded synthetic data).

| # | Pre-registered check | Result |
|---|---|---|
| 1 | tier boundaries 49/50, 99/100, 999/1000; 600 stock forecasts → n_eff 50 → `shrink` | pass |
| 2 | shrink: n = 75, k = 75 → w = 0.5; p 0.9, p̄ 0.5 → 0.7 | pass |
| 3 | Platt recovers a = 0.5, b = 0.3 (n = 20,000, ±0.05) | pass |
| 4 | temperature recovers T = 2 (±0.15) | pass |
| 5 | PAV on y = 1,0,1,0,1: blocks {0.1,0.2}→0.5, {0.3,0.4}→0.5, {0.5}→1; monotone on a distorted sample | pass (block values as registered; between block centres the applied map is linear, so p = 0.4 maps to 2/3, as the table's "linear between block centres" says) |
| 6 | Platt does not worsen the fitted-sample Brier on a distorted sample | pass |

Exposure: `GET /api/calibration` now carries `policy` on every group (tier, n, effective n, overlap, description,
the n at which the next tier starts); the Signals page shows it in the group facts. Nothing applies a calibrator to
displayed probabilities: the live ledger has far fewer than 50 resolved forecasts per group.

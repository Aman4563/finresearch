# E-IPO-1 results: post-hoc calibration of the IPO listing model

Generated 2026-10-01 by `finresearch.evals.ipo_calibration` from `ipo_history.jsonl` (sha256 5abee99809574022…). Pre-registration: `PREREG.md` (committed before this run).

## Brier skill vs the base-rate table, by test year (positive = better than the table)

| Year | n test | n pool | Table Brier | Raw model | T | S | P |
|---|---|---|---|---|---|---|---|
| 2019 | 10 | 50 | 0.1268 | 0.6200 | 0.5626 | 0.3689 | 0.5889 |
| 2020 | 9 | 60 | 0.0663 | 0.6783 | 0.6315 | 0.4341 | 0.5536 |
| 2021 | 40 | 69 | 0.1228 | -0.1810 | -0.1677 | -0.0533 | -0.2129 |
| 2022 | 32 | 109 | 0.2263 | -0.3114 | -0.1852 | 0.0020 | -0.1914 |
| 2023 | 46 | 141 | 0.1610 | 0.3002 | 0.2749 | 0.1759 | 0.2734 |
| 2024 | 70 | 187 | 0.1374 | -0.1364 | -0.0803 | -0.0330 | -0.0795 |
| 2025 | 81 | 257 | 0.1538 | -0.1257 | -0.1253 | 0.0197 | -0.1017 |
| 2026 (partial, not gated) | 64 | 338 | 0.1616 | 0.1485 | 0.1363 | 0.0899 | 0.1014 |

## Pooled 2019–2025 (n = 288)

| Forecast | Brier | BSS vs table | AUC | Log loss |
|---|---|---|---|---|
| Base-rate table | 0.1510 | 0 | 0.8069 | 0.4513 |
| uncalibrated model (raw) | 0.1601 | -0.0600 | 0.8250 | 0.4862 |
| temperature scaling (T) | 0.1558 | -0.0315 | 0.8202 | 0.4722 |
| shrinkage blend to the table (S) | 0.1451 | 0.0393 | 0.8327 | 0.4349 |
| Platt scaling (P) | 0.1557 | -0.0313 | 0.8227 | 0.4696 |

## Verdicts (bar: BSS > 0 in ≥ 5 of 7 years AND pooled Brier below the table's)

| Variant | Years passed | Pooled Brier better | Passes |
|---|---|---|---|
| uncalibrated model (raw) | 3/7 [2019, 2020, 2023] | no | no |
| temperature scaling (T) | 3/7 [2019, 2020, 2023] | no | no |
| shrinkage blend to the table (S) | 5/7 [2019, 2020, 2022, 2023, 2025] | yes | **yes** |
| Platt scaling (P) | 3/7 [2019, 2020, 2023] | no | no |

## Fitted parameters by year (fitted on earlier OOF years only)

| Year | T | Platt a, b | λ |
|---|---|---|---|
| 2019 | 1.2604 | 0.7673, -0.3919 | 0.4000 |
| 2020 | 1.1411 | 0.8538, -0.4297 | 0.5500 |
| 2021 | 1.0835 | 0.8776, -0.4097 | 0.6000 |
| 2022 | 1.3499 | 0.7077, -0.1440 | 0.4500 |
| 2023 | 1.6531 | 0.5560, 0.1473 | 0.3500 |
| 2024 | 1.4659 | 0.6249, 0.1820 | 0.5000 |
| 2025 | 1.4133 | 0.6538, 0.1748 | 0.4500 |
| 2026 | 1.4262 | 0.6319, 0.3110 | 0.4000 |

## Reliability, pooled 2019–2025 (equal-count bins: mean forecast → observed)

- uncalibrated model: 0.18→0.41, 0.54→0.59, 0.84→0.72, 0.95→0.95, 0.98→1.00
- temperature scaling: 0.24→0.41, 0.53→0.57, 0.76→0.76, 0.88→0.96, 0.96→0.96
- shrinkage blend to the table: 0.35→0.38, 0.56→0.62, 0.76→0.76, 0.90→0.91, 0.97→1.00
- Platt scaling: 0.25→0.47, 0.54→0.55, 0.77→0.71, 0.88→0.95, 0.95→1.00
- base-rate table: 0.44→0.40, 0.60→0.62, 0.70→0.76, 0.89→0.91, 0.97→0.98

## Live-parity check (OFS share and Nifty 20-session return blanked at prediction) (informational: the live signal now supplies both, ADDENDUM.md)

- uncalibrated model (raw): 3/7 years, pooled Brier better: False, passes: False
- temperature scaling (T): 3/7 years, pooled Brier better: False, passes: False
- shrinkage blend to the table (S): 4/7 years, pooled Brier better: True, passes: False
- Platt scaling (P): 4/7 years, pooled Brier better: False, passes: False

## Decision

- Ship: S (mode: shadow)
- Reason: shrinkage blend to the table passed the bar; the live signal now supplies the OFS share and the Nifty 20-session return (ADDENDUM.md), so the parity check is vacuous and reported only. Coordinator decision on #151: it runs as a SHADOW test beside the base-rate call, switching only under SHADOW.md's pre-registered criterion

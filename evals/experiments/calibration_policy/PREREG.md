# Pre-registration: tiered calibration policy for the forecast ledger (research §2.6)

Registered 1-Oct-2026, before the function was tested or exposed. Issue #147 (part of #101). Evals only: no live
forecast is changed by this; `/api/calibration` only reports which tier each track record has reached.

## Policy (fixed now)

| Effective resolved n | Tier | What a calibrated forecast would be |
|---|---|---|
| < 50 | `base_rate` | the group's observed base rate p̄ only; the model's p is shown as "uncalibrated" |
| 50 – 99 | `shrink` | p′ = w·p + (1 − w)·p̄, w = n/(n + k), **k = 75** (midpoint of the 50–100 range in §2.6) |
| 100 – 999 | `platt` | p′ = σ(a·logit p + b), maximum likelihood on Platt's smoothed targets (Platt 1999; [31]) |
| ≥ 1000 | `isotonic` | pool-adjacent-violators, linear between block centres ([31]: isotonic needs ~1,000+) |

- Effective n: resolved forecasts ÷ overlap. Overlap = 12 for the stock 12-month event (a forecast can be logged
  every month for the same stock, so windows overlap by 11 months, as in the stock backtest); 1 otherwise.
- Fitting uses only forecasts logged before their outcome (the ledger's forecasts are out of sample by
  construction).

## Tests (synthetic, offline) that must pass before it is exposed

1. Tier boundaries at 49/50, 99/100, 999/1000, and effective n with overlap 12 (600 stock forecasts → 50 → `shrink`).
2. Shrink weight: n = 75, k = 75 → w = 0.5, p = 0.9, p̄ = 0.5 → 0.7.
3. Platt recovers a known distortion: outcomes drawn from q = σ(0.5·logit p + 0.3) (seeded, n = 20,000) → a ≈ 0.5,
   b ≈ 0.3 within ±0.05.
4. Temperature recovers T = 2 from outcomes drawn from σ(logit p / 2).
5. PAV output is non-decreasing and matches a hand-computed case: y = 1,0,1,0,1 on p = 0.1…0.5 → fitted
   0.5, 0.5, 0.5, 0.5, 1 (blocks {0.1,0.2}, {0.3,0.4}, {0.5}).
6. Calibration never makes the fitted-sample Brier worse than the raw forecast for Platt on a distorted sample.

## Exposure

`GET /api/calibration` gains, per group, `policy: {tier, n, n_effective, description}` (additive; nothing removed).
Nothing downstream applies the calibrator yet: the tiers are informational until a group reaches `shrink`, and
applying it to displayed probabilities is a separate, reviewed change.

## Amendment, 1-Oct-2026 (before any group reached `shrink`; no outcome was looked at)

The "overlap = 12" rule assumed one stock forecast a month. The ledger logs a forecast on every IST day a signal is
viewed (IPOs during bidding, stocks any day), so n/12 overstated the stock count and nothing at all corrected the
IPO count (one listing viewed on five days counted five times). From this date `/api/calibration` counts independent
events before computing any metric or tier (`signals.ledger.independent_events`): one forecast per IPO listing (the
latest, i.e. the last before the listing-day open) and non-overlapping 12-month windows per stock (each kept window
starts on or after the previous kept one's resolution date). `group_policy` then uses overlap 1. Test 1's
"600 stock forecasts → 50" now reads "50 independent stock events → `shrink`"; `fit_policy(overlap=12)` stays for the
monthly backtest sample. Tier boundaries, k and the calibrators are unchanged.

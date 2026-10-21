# Volatility forecast race and the VRP-conditioned seller variant (studies `vol_forecast_race`, `vrp_seller_variant`; P3.7)

Part of the vault pack (PREREG.md, #364). Scheduling (T22): the race reads the vault only after `vrp_primary`.

## 1. Race: one primary challenger

- **Target:** RV over the next h sessions, h = 5, 10, 22: Yang-Zhang on within-contract front-future OHLC (NIFTY;
  BANKNIFTY secondary), in the RV's clock (calendar days / 365 until P3.1 is adopted).
- **Primary challenger:** log HAR-IV, no event dummies:
  ln RV_t+1:t+h = b0 + b_d ln RV_t + b_w ln RV_t^(5) + b_m ln RV_t^(22) + b_iv ln IV^2_t + e, bias-corrected by
  exp((1/2) sigma_e^2).
- **Benchmark:** the bias-corrected, horizon-matched market forecast RV-hat = exp(a + b ln w_h + s^2/2), w_h = total
  implied variance to the horizon interpolated across the bracketing expiries; a, b on the expanding window.
- Expanding window, monthly refits, purge t + h <= refit day; the first 500 sessions of the vault are burn-in.
- **Loss:** QLIKE L = RV / sigma-hat^2 - ln(RV / sigma-hat^2) - 1 (primary); MSE secondary.
- **Tests:** DM on non-overlapping h-session windows, Holm across the 3 horizons; 90 % model confidence set (Hansen,
  Lunde & Nason) over {challenger, benchmark, secondary models}; Mincer-Zarnowitz reported.

## 2. Success and adoption (all must hold)

The primary challenger beats the benchmark on QLIKE at Holm p < 0.05 at >= 2 of 3 horizons, in BOTH 2008-2021 (vault)
and 2022-2026, and is in the 90 % MCS. Every other estimator (Parkinson, Garman-Klass, Rogers-Satchell, GARCH/GJR,
EWMA lambda = 0.94, plain HAR) is secondary and cannot be adopted. Forecast skill alone never unlocks ENTER.

## 3. Conditional registration (run only if its condition holds)

`vrp_seller_variant` runs only if `vrp.md`'s prerequisite holds for its base spec (`nifty_ic_w`): its after-cost
expectancy is > 0 in the `library_v1_same_print` vault read and in the stored 2022-26 window. Otherwise it is never
run and its id stays unread.

## 4. The variant (VARIANTS_TRIED 8 -> 9 when it is added to a specs file)

- Base: `nifty_ic_w` exactly as registered (deltas, wings, exits, IV-percentile filter, sizing).
- Added entry condition: expected VRP_t > the median of all prior expected VRP values on the expanding window
  (point-in-time; no entry before 52 prior weekly origins). Expected VRP_t = sigma^2_MF,t - E-hat_t[RV over the
  sessions to the weekly expiry], E-hat from the race's primary challenger (log HAR-IV) at h = 5, refit as in §1.
  Unknown expected VRP = no entry.
- Vault period: NIFTY weeklies from Feb-2019 [verify] to 31-Dec-2021, same-print and next-print fills (the engine
  pin applies). Judged by the library criteria (`library_v1.md` §2, sub-period rule included) on the vault AND on a
  pre-registered forward paper period (registered before the variant's first paper trade); 2024-26 is reported as
  seen. Sign consistent across years. Expected power: low (about 150 weekly expiries, half of them entered at most).

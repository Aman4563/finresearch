# Pre-registration: the volatility forecast race (P3.7, #390)

Registered 22-Oct-2026, before any race code that can read a stored session existed: no `fno_bhav` or `fno_iv_eod`
row has been queried for this race, and the 2008-2021 vault is not loaded anywhere this project controls. The
registration is `src/finresearch/volrace/specs.py` (`vol-forecast-race-v1`); its sha256
`5f04e07945d85ffcae2bb2df2fc122af648d585e6e3a8724e1204d9889eed45c` is pinned in `volrace/prereg.py`, and the race
refuses to run while the two differ. Every numeric setting is read from the module that uses it (`volrace/race.py`,
`fincalc/realvol.py`, `fincalc/taildist.py`, `fincalc/fno_history.py`), so editing one moves the hash. This file
restates it in prose; where they differ, `specs.py` wins. It is never edited: changes go to `ADDENDUM.md` as post hoc,
with a new `SPEC_VERSION` and hash committed before the changed race reads a stored session.

This race implements the vault study `vol_forecast_race` (`evals/experiments/vault/vol_race.md`, sha256
`8434113de0316c730e117977ce1436ab32a4d9f04aa4b7139fd922c1b682f01f`, sealed in the vault pack #364) and fixes what
that file leaves open. It does not touch the card horse race or its addenda, searches no strategies, and adds nothing
to `VARIANTS_TRIED`. The VRP-conditioned seller variant (`vrp_seller_variant`) is not registered or run here.

## Target and data

- **Bars:** the NIFTY index future (BANKNIFTY reported only) from `fno_bhav`. Each session's front contract is its
  nearest expiry on or after the session; the bar is that contract's open, high, low and close, with the previous
  close taken from the SAME contract on the immediately previous stored session. A missing or non-positive price, a
  high below max(open, close) or a low above min(open, close) makes the bar invalid: every estimate over a window that
  holds it is unknown and counted, never dropped.
- **Target:** y(t, h) = h x Yang-Zhang per-session variance over sessions t+1..t+h (k = 0.34 / (1.34 + (h+1)/(h-1))),
  total log variance over the horizon, at h = 5, 10 and 22 sessions.
- **Implied variance:** each session's per-expiry ATM Black-76 IVs from the stored option settles
  (`fno_history.day_summary`, the engine behind `fno_iv_eod`). w_h = total implied variance to the calendar date of
  session t + h (days / 365), linear in total variance between the bracketing expiries, flat at the shortest
  expiry's IV before it, unknown beyond the longest. The issue's sigma^2_MF (P3.6) does not exist yet; ATM IV stands
  in for it. The NSE session calendar is published in advance, so the date of session t + h is known at the origin.

## Models (every one is reported)

| key | forecast of y | role |
| --- | --- | --- |
| `har_iv` | ln y = b0 + b_d ln d_t + b_w ln d^(5) + b_m ln d^(22) + b_iv ln IV^2_t, exp(fit + s^2/2) | **primary challenger** |
| `mkt` | ln y = a + b ln w_h, exp(fit + s^2/2) | **benchmark** |
| `har` | HAR without IV | secondary |
| `iv_raw` | w_h unadjusted | secondary |
| `ewma` | h x RiskMetrics EWMA (lambda 0.94) of close-to-close returns | secondary |
| `garch`, `gjr` | GARCH(1,1) / GJR-GARCH(1,1) QMLE, sum of the h-step variance path | secondary |
| `cc22`, `pk22`, `gk22`, `rs22`, `yz22` | h x the trailing 22-session close-to-close, Parkinson, Garman-Klass, Rogers-Satchell, Yang-Zhang variance | secondary |

d_t = (ln O_t / previous close)^2 + Rogers-Satchell_t; d^(5), d^(22) are its 5- and 22-session means.
IV^2_t = w_h / tau_h (horizon-matched, annualised). No event dummies.

**Fitting.** Refit on the first session of each calendar month at or after the burn-in; forecasts from one refit to
the next use its fit. Training origins s satisfy s + h <= the refit session (the purge), expanding, at most 5,000 of
the latest. OLS, s^2 = SSR / (n - p). GARCH-type models refit on the same days on the last 1,000 known returns
(Gaussian QMLE, zero mean, arch's backcast, Nelder-Mead as in `taildist`); between refits the filter runs to the
origin. A model whose fit or input is unknown gives no forecast (None), never 0.

## Losses and tests

- **QLIKE** L = y / f - ln(y / f) - 1 is primary; MSE is secondary.
- Scored on **non-overlapping windows**: origins t0, t0 + h, ... from the first origin at or after the burn-in where
  the target, challenger and benchmark are known.
- **DM** with the HLN correction (`direction.metrics.diebold_mariano_hln`, h = 1 on that grid), one-sided, benchmark
  QLIKE minus challenger QLIKE, n >= 10; **Holm** across the three horizons, per window.
- **90 % MCS** (Hansen, Lunde and Nason 2011, T_max) on QLIKE over all twelve models, on the grid origins where every
  model is known (the others counted); stationary bootstrap, mean block 5 windows, 10,000 replicates, seed 390.
- **Mincer-Zarnowitz** y = a + b f per model, reported only.

## Windows and power

| window | sessions | burn-in | access |
| --- | --- | --- | --- |
| vault | 1-Jan-2008 to 31-Dec-2021 | 500 (vol_race.md) | one logged read through `vault.guard.vault_read("vol_forecast_race", ...)`, after `vrp_primary` |
| recent | 1-Jan-2022 to 31-Dec-2026 | 250 | the app database; labelled seen (read by earlier studies, never for this race) |

Power, stated up front: the recent window gives at most about 190, 95 and 43 non-overlapping windows at h = 5, 10 and
22 (fewer if the stored history starts after January 2022); the vault about 590, 295 and 134. At h = 22 the recent
window is thin.

## Adoption (all must hold)

The primary challenger is adopted only if, in BOTH windows, it beats the benchmark on QLIKE at Holm-adjusted DM
p < 0.05 AND is in the 90 % MCS at >= 2 of the 3 horizons, and at least two of those horizons are the same in both
windows. Every other model is secondary and cannot be adopted; BANKNIFTY is reported only. Forecast skill alone never
unlocks ENTER and changes no card.

## Not in this registration

Intraday RV and bipower variation on 1-minute bars (P1.10's data does not exist yet; `realvol` has the estimators),
the nightly materialisation and vol-cone job, the VRP-conditioned seller variant and its `VARIANTS_TRIED` bump.

## Expected result, stated before the read

IV30 already beats RV20, so any gain of HAR-IV over a bias-corrected IV forecast is probably modest; the most likely
outcome is that the challenger does not pass. That is a prior, not a claim.

# Pre-registration: PoP and tail calibration on the vault (study `horse_race_calibration`; P3.4 through P2.2)

Registered 22-Oct-2026 in the vault pack (`evals/experiments/vault/PREREG.md`, #364), before any 2008-2021 file was
loaded. This is P2.2's vault run and P3.4's primary test; one logged read.

## Models (physical distribution of the terminal move)

z_i = [ln(S_T,i / F_i) + (1/2) sigma_i^2 T_i] / (sigma_i sqrt(T_i)), sigma_i = ATM IV at the forecast origin.

- **M0 (current):** lognormal with sigma = RV20 of front-future closes and drift = r (`signals/fno.py`, the card's
  real-world case).
- **M1:** lognormal at ATM IV (forward-centred, sigma = sigma_i).
- **M2:** IV-scaled empirical: S_T = F exp(-(1/2) kappa sigma^2 T + sigma sqrt(T) z*), kappa = E[z^2] on the
  expanding window, z* from a stationary block bootstrap over past expiries (mean block 3 expiries), by tenor bucket
  (<= 14 DTE; 15-45 DTE), no GPD tail in the primary run. Expanding window: only expiries settled before the origin.
- **M3:** GJR-GARCH(1,1) filtered historical simulation on daily front-future log returns (expanding-window MLE,
  refit monthly), standardised residuals resampled in 5-day blocks, rescaled so E[sum r^2] = kappa sigma_IV^2 T.

M2 and M3 are scored only once their expanding window holds >= 36 settled expiries of the bucket; M0 and M1 are
scored on exactly the same events (paired). Burn-in events are reported, not scored.

## Events (one per expiry and underlying)

Forecast origin: monthly expiries = the session nearest to 30 calendar days before expiry (earlier on a tie);
weekly NIFTY expiries (from Feb-2019 [verify]) = 5 sessions before expiry. Underlyings: NIFTY 2008-2021, BANKNIFTY
from the first vault year with >= 10 traded OTM strikes per side on >= 90 % of origins.

- Binary events: S_T beyond the 10-, 16- and 25-delta put and call strikes (delta at ATM IV at the origin): 6 events.
- Each registered seller spec's PoP at its own entry rule (outcome = 1{P&L after costs > 0} under its exits).
- The continuous terminal log-return ln(S_T / F) (for CRPS, PIT).
- Also scored for P2.2: the card's current forecasters p_RN-ATM, p_RW20, p_RW60, p_gate and a trailing base rate.

## Scores and tests

- **Primary:** CRPS of ln(S_T / F), each challenger (M1, M2, M3) against M0, Diebold-Mariano with the HLN correction
  on per-expiry differentials, one-sided, Holm across the 3 challengers x underlyings.
- Secondary: Brier and log score on each binary event (DM-HLN, Holm across events); Murphy decomposition.
- **Tail coverage:** Kupiec POF at 5 % and 95 % of the terminal distribution; Berkowitz LR on inverse-normal PIT
  values; Christoffersen independence. The 1 %/99 % Kupiec test runs only where >= 5 exceedances are expected
  (n >= 500 independent events at 1 %). Kupiec, Christoffersen, Acerbi-Szekely Z2 run only on samples of >= 100
  independent events; smaller samples are reported descriptively.
- **EV calibration:** PnL_i = a + b EV_i + e per recipe, expiry-clustered SE; calibrated iff CI(b) contains 1 and
  CI(a) contains 0.
- Report E|z| / sqrt(2/pi) beside E[z^2] for near expiries.

## Adoption (all must hold, on the vault)

A challenger replaces M0 only if it beats M0 on CRPS at Holm p < 0.05 AND passes tail coverage (Kupiec 5 %/95 %
p >= 0.05 in both tails AND Berkowitz p >= 0.05). Otherwise M0 stays and model EV stays informational (MF1).

## Periods

Primary: the vault, 2008-2021, one read. Secondary: the 2022-26 walk-forward at h = 5, 10 and 21 sessions, labelled
seen (it cannot adopt a model by itself). Reported by era.

## Known limits stated before the read

Overlapping windows: monthly origins are non-overlapping by construction; weekly ones are too. BANKNIFTY's early years
are thin. Index closes before 2013 come from the front future (T12). The crash outcomes are public.

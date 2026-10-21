# Stress uses of the vault (studies `crisis_day_replays`, `stress_start_day_scan`, `risk_model_samples`; P4.1, P3.4)

Part of the vault pack (PREREG.md, #364). Each id is one logged read.

## `crisis_day_replays` (after the data audit passes)

The named crisis days, public knowledge, replayed on the book of the day the replay is run, as in P4.1's replay
library: 24-Oct-2008 and 23-Mar-2020 single-day moves (front future before 2013, else index log return; IV30 change;
the same expiry's 25-delta skew change from the vault rows), and the multi-week episodes Oct-2008 and Mar-2020 as
daily paths at h = 21 sessions (legs expiring within h valued at intrinsic). Only these two days and two episodes
are extracted; the read stores their shock vectors and nothing else.

## `stress_start_day_scan` (only after `horse_race_calibration` and `library_v1_same_print` have been read, T14)

Historical-scenario revaluation over every vault start day s: dS = S (S_s+h / S_s - 1), dsigma = IV30_s+h - IV30_s,
h = 1 and 5 sessions -> the 99th-percentile loss and the 10 worst dates for the book. Published exceedances of the
h = 1 and h = 5 grids against the realised 1- and 5-session position losses include 2008 and 2020.

## `risk_model_samples` (only after `horse_race_calibration` has been read)

Vault-derived z-samples (P3.4's z_i per expiry and bucket) and daily shock vectors (front-future log return, IV30
change) stored for the risk models of P3.4 and P4.1. A model using them is adopted only under
`fno_pop_calibration/PREREG.md`'s adoption rule. No strategy P&L is computed from this read.

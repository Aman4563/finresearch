# Addendum (1-Oct-2026, after the first run): executing the pre-registered follow-up

Written after `RESULTS.md` was first committed (commit "E-IPO-1 results: shrinkage blend passes the bar, fails the
live-parity check"). `PREREG.md` is unchanged.

## What the first run found

- Variant S (shrinkage blend to the table) passed the pre-registered bar: Brier skill vs the table > 0 in 5 of 7
  years (2019, 2020, 2022, 2023, 2025) and pooled 2019–2025 Brier 0.1451 vs the table's 0.1510 (skill +0.039,
  n = 288). T and P failed (3 of 7 years, pooled Brier worse).
- S failed the deployment check (4 of 7 years) when the OFS share and the Nifty 20-session return were blanked at
  prediction time, as the live signal did.

## What the PREREG says to do

"If a variant passes the bar but fails the deployment check: record it; the signal stays on the base-rate table; the
follow-up is to fill the two live features (#137 gap) and re-run this pre-registration unchanged."

## What was done

1. The live IPO signal now computes both features the way the harvest does (`signals/ipo.py::_live_features_full`):
   the OFS share with the harvester's own parser (`evals.ipo_history.parse_issue_size`) on the same NSE field
   (ipo-detail issue information, "Issue Size"), imputed with the training median when unparseable exactly as in
   training; and the NIFTY 50 20-session return with the harvester's `nifty_return` on NSE's index history.
2. With both features supplied, the set of blanked features the deployment check was defined on is empty: the check
   is vacuous. It is still computed and reported (`results.json → live_parity`, marked informational), and it still
   says 4 of 7 for the blanked case. The experiment code records this with `LIVE_FEATURES_FILLED = True`.
3. The rest of the run is unchanged (same data, same folds, same variants, same bar, same priority order T, S, P),
   so S ships on the bar alone. The deployed λ is refitted on every real-feature OOF forecast 2017–2026.

## What this costs, stated plainly

- The deployment check was also the only backstop against the multiplicity of three variants tested against an
  uncorrected bar. That backstop is gone. S passed 5 of 7 years, the minimum, and its narrowest passing year (2022)
  had skill +0.002. The signal's validation text says so.
- In S's favour (reported, not gating): it is the best-calibrated forecast in the pooled reliability bins, it also
  beats the table on AUC (0.833 vs 0.807) and log loss (0.435 vs 0.451), and with λ between 0.35 and 0.60 in every
  fold it stays close to the table the app already shows.
- Timing: training used the Nifty close on the issue's last day; on the closing day the live signal has the previous
  session's close. The signal carries a caveat.

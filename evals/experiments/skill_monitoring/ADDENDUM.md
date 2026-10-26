# Addendum: live skill monitoring, version `skill-monitor-v2` (P2.3, #378), post hoc

Registered 26-Oct-2026 as a new `SPEC_VERSION`, post hoc to [PREREG.md](PREREG.md). PREREG.md and v1
(`skillmon/specs.py`, sha256 `48a6ef33ede244488a3a489ed9ddc781b2443a92dc24c113756aeac255ea3de8`) are unchanged, and
every forecast-cell check still runs under v1. The v2 registration is `src/finresearch/skillmon/specs_v2.py`; its
sha256 `0ab79edc34b1ef38f671b61dc12b24ba9087c454b26736dc2e3a9b15d6e6e2ee` is pinned in `skillmon/prereg.py`
(`V2_HASH`) and was committed before any code read a stored F&O session, a VaR-ledger row or an `ipo_history` row for
it. Where this file and `specs_v2.py` differ, `specs_v2.py` wins. A change is a new version, never an edit.

v2 wires three items v1 registered for later (`specs.Later.psi`, `.pinball`, `.risk_triggers`) and fixes their open
choices. Nothing here changes a call, sizes a trade or promotes a model.

## 1. Feature drift (PSI)

- PSI = Σ_b (a_b − e_b) ln(a_b / e_b) over the bins' shares, each share floored at 0.0001 (`skillmon.stats.psi`).
- Bins: the distinct values among the reference sample's 10th, 20th, ..., 90th percentiles (numpy linear
  interpolation) are the edges; bins are right-closed, (−∞, e1], (e1, e2], ..., (ek, +∞). At most 10 bins; a 0/1
  flag's values fall in 2 of them.
- Levels (issue #378): PSI >= 0.10 is **watch**, PSI >= 0.25 is **act** (an alarm), below 0.10 is ok.
- Samples: the reference is every known value of the input on the stored sessions inside the reference window (at
  least 250, else insufficient data); the live sample is the input's last 250 known values on the stored sessions
  after the reference window's end (every stored session, not only the days a forecast was logged; fewer: insufficient
  data). A panel that cannot be read is unknown. Neither is ever read as ok.
- Panel: `direction.data.load(symbol)`, the app's own stored F&O history, read only; the inputs come from
  `direction.features.build_features` on it.

| model | inputs | underlyings | reference window | why this window |
| --- | --- | --- | --- | --- |
| direction (L2 logistic) | all 28 registered features (`direction/specs.py` `FEATURES`) | NIFTY, BANKNIFTY | 2022-01-01 to 2024-06-30 (`TRAIN_START`..`IS_END`) | the registered training window; the live logistic refits monthly on an expanding window, so this is drift from the registration's training data, not from the latest refit |
| F&O card | `iv_30`, `rv_20`, `rv_60`, `iv_pctile` (`iv_stats_sessions` over 252 sessions, the recipes' `iv_pct_min` filter), `iv_rv_ratio` (`iv_30 / rv_20`) | NIFTY, BANKNIFTY | 2022-04-01 to 2024-06-30 (the card horse race's `design` window) | the card's probabilities are registered in `card-horse-race-v1` (incumbent `p_rw`); the recipes' in-sample window starts three months earlier, on 2022-01-01 |

The card's inputs here are the stored daily series behind its two probabilities and its IV filter, not the option
chain or NSE closes the live card read.

- Brief: one alarm per model and underlying with any input at act, naming those inputs by PSI, largest first; one
  info line counting the inputs on watch and one counting those with insufficient data.
- **Known limit.** Under an i.i.d. no-drift null PSI is about (1/n_live + 1/n_ref) χ²(bins − 1) (Yurdakul 2018,
  Western Michigan University dissertation [U]). Simulated with 600 reference and 250 live values in 10 bins
  (`specs_v2.Drift.known_limit`): mean 0.052, 4.2 % of draws reach 0.10, none of 4,000 reaches 0.25. Serially
  correlated inputs (long returns, moving averages, volatilities) hold far fewer independent values and cross both
  levels more often without any break. If 2025-26 volatility or the expiry calendar differs from 2022-24 (BANKNIFTY's
  weekly expiries ended on 20-Nov-2024), several inputs will read act from the first night, and the alarm stands
  while they differ: that is what the rule measures, not a fault.

## 2. IPO ranges: pinball loss and 80 % coverage, with acceptance (d)

- Walk-forward: `evals.ipo_model`'s split, unchanged: its `usable()` mainboard rows, train on listing years < t and
  test year t for t = 2019-2026, fitted by its `fit()`, `predict()` and `table_forecaster()`. A year with fewer than 30
  training rows or no test issue is skipped and reads insufficient data.
- Two forecasters, each judged on its own: **model**, the quantile regression's q10, q50, q90; **table**, the QIB band ×
  regime cell's empirical 10/50/90 % returns, the range the IPO card shows while the model has not passed its gate.
- Outcome y: `return_open` (the listing-day open over the issue price, minus 1), not winsorised.
- Pinball loss per year and τ in {0.1, 0.5, 0.9}: the mean of (y − q)(τ − 1{y < q}) over the year's test issues
  (`skillmon.stats.pinball`), descriptive.
- Coverage: inside = the test issues with q10 <= y <= q90; its share and the exact two-sided binomial p against 80 %
  (`horserace.scores.band_coverage`). A year is within when |100 × inside − 80 × n| <= 7 × n, in integers (73 of 100
  is within, 72 is not).
- **Acceptance (d), per forecaster:** pass when the 80 % range is within ±7 pp in at least 5 of 7 complete years
  (2019-2025); fail when the years within plus the years with insufficient data are fewer than 5; otherwise
  insufficient data. 2026 is partial: reported, never counted.
- Read only, for the owner: `finresearch forecasts ipo-intervals` (`--data` reads a JSONL export instead of the
  database, `--json` writes the full report). It was not run on real data for this registration.
- **Known limit.** A perfectly calibrated 80 % range lands within ±7 pp with probability 0.60 at 20 test issues,
  0.75 at 30, 0.79 at 50 and 0.94 at 100 (exact binomial; not monotone in n), so a small year can miss without any
  miscalibration, and (d) can fail for a calibrated range.
- **Seen data.** `src/finresearch/evals/artefacts/ipo_model_walkforward.json` (committed 30-Sep-2026) already holds
  the model's per-year p10-p90 coverage and pinball loss from this same walk-forward. It was not opened for this
  registration, whose numbers (80 %, ±7 pp, 5 of 7, τ) are the issue's; still, (d) for the model is not a blind test,
  and its result should be read as on seen data. The table's coverage is not in that artefact.

## 3. Risk-model trigger: Kupiec, "not for limits"

- Per quarter: Kupiec (1995) proportion of failures, two-sided χ²(1) (`horserace.scores.kupiec`); a tested quarter
  fails at p < 0.05.
- Quarters: the calendar quarter of each forecast's own day (`inputs.as_of`, an IST date), one outcome per day. A
  quarter is tested only once it ended before today (IST) and none of its rows is still open (until then it is
  pending, never a pass), and only with at least 50 resolved days (about 80 % of its ~62 NSE sessions); fewer is
  insufficient data. Void rows never count: an unknown return is not "no exception".
- The mark: **3 consecutive** failing quarters anywhere in the record make the model **"not for limits"**, and the
  mark stays (no reset; only a new version can lift it). Otherwise: "insufficient data" with fewer than 3 tested
  quarters; "unknown" when a run of 3 quarters without a pass includes an untested or pending quarter (it could have
  been a failing run); else "not triggered".
- Models: `risk_var_ledger` (the forecast ledger's logged 1-day 95 % VaR rows, `portfolio/var_ledger.py`) and
  `risk_var` (the same historical 250-day estimator, `portfolio.analytics` `VAR_WINDOW`), which is scored on the
  ledger's logged figures, never on its walk-forward re-estimate (a backdated import rewrites that).
- **Known limit.** At 62 days and a 5 % rate a quarter fails with 0 or >= 7 exceptions: 7.6 % of quarters under a
  correct VaR (three in a row about 4e-4) and 43 % when the true rate is 10 % (three in a row about 8 %). The test is
  two-sided, so three quarters without any exception also mark the model (an overstated VaR makes limits too tight).
  Nothing in the app sets a limit from VaR today: the mark is shown in the model inventory and the brief.

## Where it shows

The nightly skill pass (`monitor/skill.py`) stores drift and the risk marks under its report's `v2` key; the brief
lists their alarms and one line per kind of unchecked input, and says both are unchecked for a report from before v2.
`finresearch forecasts skill` prints them on read. The marks are also computed on read in the model inventory (`GET
/api/models` `limits`, `finresearch models list`). The IPO ranges are only on read (`forecasts ipo-intervals`).

## Still later (not in v2)

Adaptive conformal intervals, the pre-defined regimes with cluster-bootstrap heterogeneity tests, and the P&L CUSUM
against the backtest expectancy: each in its own later version, its hash committed before it reads anything.

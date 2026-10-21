# Vault pre-registration pack (issue #364, roadmap P1.2)

Registered 22-Oct-2026, **before any 2008-2021 index-options or index-futures file has been downloaded or loaded**
anywhere this project controls (the app database, `finresearch_fnohist`, data/). No vault figure exists. This commit
is pushed to the public GitHub remote before the backfill (#365, P1.3) starts: the remote's timestamp is the external
record that every test below was fixed before the data could be seen.

New files only: no existing spec, PREREG.md or criterion is edited. `fno_backtest/PREREG.md` and
`backtest/specs.py` (hash `6d92407630d7…`, `backtest/prereg.py`) stay exactly as registered on 12-Oct-2026.

## 1. What the vault is

The vault = NSE's daily F&O bhavcopy rows for index options and index futures (NIFTY and BANKNIFTY; FINNIFTY from
its first contracts in 2021) with a trade day from **1-Jan-2008 to 31-Dec-2021**, plus everything derived from those
rows (fno_iv_eod, fno_contract, settles, z-values, shock vectors, P&L). It is the only clean out-of-sample period left
that contains crises (Oct-2008, Mar-2020). 2022-2026 has been read many times and is "seen".

The vault lives in its own database (P1.3), never in the app database. Pre-registration prevents tuning on the vault,
not knowledge of it: the 2008 and 2020 crash outcomes are public, so the vault tests calibration and survival, not
discovery.

## 2. Access rule (enforced in code: `finresearch.vault`)

1. **Only registered studies read the vault.** A read is opened by `vault.guard.vault_read(study_id, study_hash)`.
   The guard recomputes the study's hash from the committed files and refuses (fails closed) when the id is not
   registered, when the presented hash or the recomputed hash differs from the one recorded in `vault/pack.py`, when
   this pack's own hash differs from the recorded pack hash, when a prerequisite study has not been read, when an
   engine-pinned study's engine hash differs from the pin (or no pin exists), or when the read log cannot be written.
2. **One logged, irreversible read per study per spec hash.** The log row (`vault_read`: study id, study hash, pack
   hash, engine hash, git SHA, time, purpose) is committed *before* the vault connection is handed out. A read that
   then crashes is still spent. A second read of the same study hash is refused. The only repeatable study is the
   aggregate data audit (`vault_data_audit`), each of whose reads is logged with a running number.
3. **No later registration.** Adding or changing a study changes the pack hash. Once any read is logged, the guard
   refuses every read made under a different pack hash. A study registered after the first vault read may use only
   forward data (days after its own registration commit).
4. **The Strategy Lab never reads the vault.** It has no study id. Its holdout is forward data only.
5. **Every vault read counts in the trial count N** of the Lab and of any multiple-testing correction that spans
   studies (`vault.guard.reads_count()`), the data audit excepted (aggregates, no hypothesis).
6. **The app never sees the vault by accident.** `backtest.dbhistory.DbHistory` refuses any database whose stored
   F&O history starts on or before 31-Dec-2021 unless it was opened with a live grant from the guard, and
   `fnohist.store.backfill` refuses vault-period days. Role grants on the vault database (P1.3: an aggregate audit
   role, a study role; the app and Lab roles get no CONNECT) keep app code out but do not bind a superuser
   (`select rolsuper from pg_roles where rolname = current_user`), so the seal is the commit order plus this log,
   optionally with an encrypted dump.
7. **Engine pin.** Studies that replay strategies through `backtest/` read only with a pinned engine:
   `vault.pack.engine_hash()` = SHA-256 over the sorted relative paths and bytes of `src/finresearch/backtest/*.py`,
   `src/finresearch/fincalc/options.py` and `src/finresearch/fincalc/charges.py`. The pin
   (`vault/engine_pin.py`) is committed and pushed only after P0.2's settlement rule (#352, merged), P2.7(a)'s
   next-print fill variant (#382) and P1.3's dated lots and charges (#365) are merged, and before the read. The git
   SHA of each read is logged beside the engine hash. An engine change after a read can re-read the vault only as a
   disclosed ADDENDUM that cannot create a candidate (the guard refuses it until an addendum mechanism is registered).
8. **Red team.** Before each read and each adoption decision, a separate Claude session given only this pack, the
   study's file and the code reviews the run; its notes are filed beside the RESULTS.

## 3. Registered studies

| study id | roadmap | file | prerequisite reads | engine pin |
|---|---|---|---|---|
| vault_data_audit | P1.3 | audit.md | none (repeatable, aggregates only) | no |
| library_v1_same_print | P1.2 §2 | library_v1.md | vault_data_audit | yes |
| library_v1_next_print | P2.7(a) | library_v1.md | vault_data_audit | yes |
| horse_race_calibration | P2.2 + P3.4 | ../fno_pop_calibration/PREREG.md | vault_data_audit | no |
| vrp_primary | P3.6 | vrp.md | vault_data_audit | no |
| vol_forecast_race | P3.7 | vol_race.md | vrp_primary | no |
| vrp_seller_variant | P3.7 (conditional) | vol_race.md | library_v1_same_print, vrp_primary, vol_forecast_race | yes |
| event_vol_fairness | P3.8 | event_vol.md | vault_data_audit | no |
| event_skip_variant | P3.8 (conditional) | event_vol.md | library_v1_same_print, vrp_primary | yes |
| risk_budget_counterfactual | P4.2 Stage 2 | risk_budget.md | library_v1_same_print | yes |
| crisis_day_replays | P4.1 | stress.md | vault_data_audit | no |
| stress_start_day_scan | P4.1 | stress.md | horse_race_calibration, library_v1_same_print | no |
| risk_model_samples | P3.4 / P4.1 | stress.md | horse_race_calibration | no |

Derived without a read of their own (computed from a registered read's stored output): the kill criteria (§4) and
P3.4's drawdown-band test (from `library_v1_same_print`).

Anything not in this table (P3.3 smile models, P3.5 path probabilities, P3.9 context panel, Lab variants, the post
hoc IV-tercile split of `fno_backtest/RESULTS.md`, any new seller recipe) may not read the vault. A "prerequisite
read" is enforced by the guard as a logged read; a conditional study must also find its stated condition true in the
prerequisite's stored output, or it is not run (and its id stays unread).

## 4. Research kill criteria (roadmap Section 6.4), evaluated on `library_v1_same_print`

Seller specs = the four registered sellers of `backtest/specs.py`: `nifty_pcs_m`, `nifty_ccs_m`, `banknifty_ic_m`,
`nifty_ic_w`. Real-money F&O suggestions are **retired for good**, and P3.3, P3.5 and the seller-probability parts of
P3.6-P3.9 are parked, if EITHER holds:

- **K1 (no persistent seller):** no seller spec has expectancy per trade after costs > 0 in at least 60 % of its
  qualifying vault calendar years (the sub-period rule of `library_v1.md` §3, with its qualifying-year rule).
- **K2 (tails understated):** for ANY seller spec, the vault ES5 is more than 50 % worse than its in-sample ES5:
  `ES5_vault < 1.5 x ES5_IS`, where ES5 = the mean of the worst ceil(0.05 n) trade P&Ls in rupees per trade (a
  negative number), `ES5_IS` is computed by the same code on the registered in-sample window (1-Jan-2022 to
  30-Jun-2024) of the same engine version, and both use the registered slippage (x1). A spec with fewer than 20 vault
  trades has an unknown ES5; unknown counts as "understated" (unknown is never safe).

The same two tests are reported on `library_v1_next_print` too; the kill fires if either read triggers it. A result
that does not trigger the kill unlocks nothing: a candidate still needs every library criterion on the vault AND on
the published 2024-26 out-of-sample window, which no spec met (`fno_backtest/RESULTS.md`, 0 of 8).

## 5. Statistical bar and N

N for the library's deflated Sharpe stays 8 (VARIANTS_TRIED; the next-print read is a registered sensitivity and does
not bump it). The two conditional seller variants, if run, each bump VARIANTS_TRIED by one (to 9, then 10) in their
own specs file. For orientation (P2.6's corrected form, V = 1/(T-1), N = 8): the deflated-Sharpe bar SR* is about
2.07 annualised at T = 570 days and about 0.85 at T = 3,400; the registered criterion is unchanged (V = the variance
of the trials' Sharpe ratios) and P2.6's form is reported beside it, never gating.

## 6. Edge-case decisions fixed now

- **Eras.** Every vault result is reported by era: 2008-2015 (monthly only), 2016-2021 (with BANKNIFTY weeklies from
  May-2016 and NIFTY weeklies from Feb-2019 [verify dates in P1.3]), and the stored 2022-26 for comparison.
- **Weekly specs** run only where the weekly contracts exist: `nifty_ic_w` on the vault = the first NIFTY weekly
  expiry (Feb-2019 [verify]) to 31-Dec-2021.
- **BANKNIFTY** runs over the whole vault; the engine's registered liquidity rules (short leg >= 10 contracts traded,
  wing traded) skip illiquid days, so early years simply contribute few or no trades. FINNIFTY (2021 only) is a
  cross-market check, reported, never gating.
- **IV warm-up.** The IV percentile needs 60 sessions of history: IV-filtered sellers cannot enter before about
  April 2008, and their percentile window is shorter than a year through 2008 (as in 2022).
- **Stitching.** The vault replay ends on 31-Dec-2021 and positions open then are closed at that day's marks; no
  vault replay reads 2022 rows. The 2022-26 figures come from the existing published runs.
- **Charges and lots** come from P1.3's dated tables; a day with no dated lot skips the trade (unknown lot = no entry).
- **Unknown is never a pass.** Any statistic that cannot be computed (too few trades, events or years) fails its
  criterion and is reported as unknown.
- **Data audit first.** Every non-audit read requires a logged `vault_data_audit` read; a study whose #365 audit
  checks failed is not run until the failure is fixed and the audit re-read (the audit is repeatable).

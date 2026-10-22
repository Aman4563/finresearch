# Pre-registration: the cross-sectional stock-option VRP study and its panel (P1.12, #374)

Registered 22-Oct-2026, before any code existed that builds the stock-option panel or the ban history: no stock-option
row outside the few held or watched stocks has been parsed into any table, and no combineoi or dated ban file has
been downloaded. The registration is `src/finresearch/optpanel/specs.py` (`stock-option-vrp-v1`) with its numeric
settings in `optpanel/params.py`; its sha256
`8a657266082489e519659bef4d3a7817462e6bf8a40b2340860cdbc6023093e2` is pinned in `optpanel/prereg.py`, together with
the sha256 of this file. The panel loader and the ban-history backfill refuse to run while either differs. Engine
settings the panel reuses (`fincalc/fno_history.py`: rate, minimum contracts, ATM band, 30-day target, RV window,
quarantine; `backtest/specs.BOOK`: slippage tiers) are hashed from their own modules, so editing one also moves the
hash. Where this file and `specs.py` differ, `specs.py` wins. This file is never edited: changes go to `ADDENDUM.md`
as post hoc, with a new `SPEC_VERSION` and hash committed before the changed panel or study reads a stored day.

The issue's start trigger is "a registered study needs it". This is that study. It is the example statistic the
issue itself states (spread = IV30 - RV20, decile sorts, the next-month ATM straddle return, a month-clustered t,
costs from P1.1), not a statistic chosen after looking at any data. It is not a library candidate: it searches no
strategies, reads no vault data (stock options are not in the 2008-2021 vault), is not added to
`vault/registry.STUDIES` and does not change `VARIANTS_TRIED`.

## 1. Question

Across NSE stock-option underlyings, does the variance risk premium proxy IV30 - RV20 at the start of a monthly cycle
predict the net-of-cost return of selling that stock's ATM straddle to expiry?

## 2. Panel definition (one row per underlying and session)

- **Source:** a re-parse of the kept raw F&O bhavcopy zips (`data/fno_bhav/<year>/`). No new download for a stored
  day. Every stock-option (STO) row, plus the index options of NIFTYNXT50 (whose futures are stored but whose options
  are not). Days in the 2008-2021 vault are refused.
- **Universe:** every underlying with stock options in that day's file, taken from the file alone. The held or
  watched list is never read, so the panel's coverage reveals no portfolio. The panel is never sent to a model.
- **Implied volatility:** `fincalc.fno_history.day_summary` on market settles (settle equal to the close, at least 10
  contracts traded). It gives the near and next ATM Black-76 IV (the first expiries at least 7 days out), IV30 by
  total-variance interpolation (flat within 7 days) and the near expiry's 25-delta skew (put IV - call IV).
- **RV20:** the 20-session close-to-close volatility of the front stock future (`fno_bhav` keeps every stock
  future). Returns are taken within one contract. The lot-change factor is applied, and any |ln| > 0.25 still left
  is excluded (#369, the F&O part of roadmap P1.7).
- **ATM straddle:** the call and put settles at the near expiry's ATM strike, with each leg's contracts.
- **OI and volume by moneyness:** open interest (in units) and contracts traded per ln(K/F) bucket, with edges at
  -0.10, -0.03, 0.03 and 0.10. Calls and puts are counted separately, over all expiries on or after the day.
- **Liquidity:** the number of option contracts traded with |K/F - 1| <= 5 %.
- **Kept rows:** full contract rows only where |ln(K/F)| <= 0.2, for the two nearest expiries on or after the day.
  Contracts with no OI and no trade are dropped.
- **Unknown values:** a value that cannot be fitted is NULL and counted, never 0.
- **Lineage:** one `dataset_load` row per file read.

## 3. Ban-period history

- **Source:** NSE Clearing's combined open-interest file (combineoi): each stock's MWPL and its combined OI at the
  end of day D. Utilisation u_D = OI_D / MWPL_D.
- **Rule** (hysteresis, per stock, in session order): a stock is banned for D+1 when u_D > 95 %, or when it was
  banned for D and u_D >= 80 %. The state is unknown until the first day that decides it (u > 95 % or u < 80 %), and
  it becomes unknown again after any missing file.
- **Validation:** the derived list for trade day D+1 is compared with NSE's dated ban notice
  (`fo_secban_DDMMYYYY.csv`) on 20 sample days drawn with a fixed seed. The owner runs this check.
- **Data rights:** both files are new NSE datasets. They are listed in `data_rights.PENDING` until the owner records
  a decision, and nothing downloads them before then.

## 4. Sample

| window | cycles | status |
| --- | --- | --- |
| in-sample | formation day on or after 1-Jan-2022 and expiry on or before 22-Oct-2026 (about 57) | stock options outside the held/watched few never parsed before |
| forward | cycles formed after 22-Oct-2026 | read once, after 24 cycles exist |

## 5. Design and test

- **Cycle:** the stock monthly expiry E_m, which is the expiry of the stock futures. The formation day t_m is the
  first stored session after E_(m-1). The position is held to E_m.
- **Signal:** s_i = IV30_i - RV20_i (vol points) at t_m.
- **Eligible stocks:** at t_m, IV30, RV20 and both straddle legs for E_m are known, and each leg traded at least 10
  contracts. The stock had a fitted ATM IV on at least 50 % of its last 60 stored sessions. Its derived ban state for
  t_m is "not banned". A banned stock or one whose state is unknown is excluded and counted.
- **Return (short straddle, per rupee of premium):** r_i = (C + P - |F_T - K| - costs) / (C + P). Entry is at the
  t_m settles. F_T is the settle of the E_m stock future on E_m, which is the final settlement price.
- **Costs (P1.1's model as it stands, `backtest.costs`):**
  - the registered slippage tiers of `backtest.specs.BOOK` on each leg at entry, with the widest tier when volume is
    unknown;
  - `fincalc.charges` for one lot at t_m's date, plus exercise STT on the in-the-money leg at expiry.
  - No stock-option quotes are recorded, so a measured cost model can enter only as a new registered variant.
- **Statistic:** per cycle, S_m = the mean r of the top quantile of s minus the mean r of the bottom quantile.
  Cycles with fewer than 50 eligible stocks are skipped and counted.
- **Test:** a one-sided t-test of mean(S_m) > 0 across cycles. There is one observation per month, so the test is
  month-clustered. Deciles are the primary sort and quintiles the secondary; Holm's correction is applied across the
  two at alpha = 0.05.
- **Power, stated up front:** with about 57 cycles, 80 % power at one-sided 5 % needs a mean S_m of about 0.33
  standard deviations of S_m.

## 6. Decision rule (all must hold)

There is evidence of a cross-sectional stock VRP premium only if all three of these hold:
1. the decile test's Holm-adjusted one-sided p < 0.05;
2. mean S_m > 0 in both halves (formation day on or before 30-Jun-2024, and after it);
3. mean S_m > 0 with slippage x 2.

The only consequence is permission to register a trading variant in its own PREREG. It changes no card, unlocks no
ENTER and adds nothing to `VARIANTS_TRIED`. Otherwise the result is reported as null.

## 7. Not in this registration

- Picking the overlay stock from the full universe (`backtest/dbhistory.py`). That file is covered by the vault
  engine pin and by the registered library specs, so the change belongs to its own registered variant.
- A stored "all stock-option rows" table. The issue does not recommend it at 3-5 GB, so the reconciliation of
  participant OI against the bhavcopy uses per-day totals the panel records instead.
- The study's runner. It is built in a later change that reads the panel through `require_registered`.

## 8. Expected result, stated before the read

The prior is low. Stock options are illiquid away from ATM, the tier costs are wide, and short-straddle returns are
fat-tailed. The most likely outcome is that the spread does not survive costs or the split. That is a prior, not a
claim.

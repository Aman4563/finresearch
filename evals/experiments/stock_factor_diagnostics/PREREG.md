# Pre-registration: factor diagnostics of the stock signal on the point-in-time Nifty 50 (#506)

Registered 23-Nov-2026, before any code that could read a price existed: no file of the price cache
(`data/cache/stock_backtest/`), no cached market data and no database row has been read for it. The registration is
`src/finresearch/factordiag/specs.py` (`stock-factor-diagnostics-v1`); its sha256
`3a536c80d8252444ef2540d23d66f5c5f6a51549c51c7728a5c85575d7a73664` is pinned in `factordiag/prereg.py` together with
this file's own sha256 and the sha256 of the two committed membership files, and the command refuses to run while any
of them differs. Every numeric setting is a constant in `specs.py` or read at call time from the module that owns it
(the harness's warm-up and costs in `evals/stock_backtest.py`, the momentum windows in `fincalc/signals.py`, the
market series and symbol map in `evals/stock_universe.py`), so editing one moves the hash. Where this file and
`specs.py` differ, `specs.py` wins. This file is never edited: changes go to `ADDENDUM.md` as post hoc, with a new
`SPEC_VERSION` and hash committed before the changed study reads a price.

## What this is, and is not

A **diagnostic of an already-registered signal**. The portfolio test in `evals/experiments/stock_pit_universe/`
answered "does the rule beat the equal-weight universe after costs?" with no. This study asks **why**, by looking at
the ranking itself: the rank information coefficient (IC) of each component score against forward returns, its decay
with the horizon, its two sub-periods, quintile returns, turnover and the break-even cost.

It adds no strategy and no trial, changes no signal, call, card or ledger row, and makes no edge claim. An IC found on
a panel that has already been read is not out-of-sample evidence. The most any outcome can justify is one further
registered test (section "What each outcome would and would not justify").

Method after the signal-evaluation chapter of S. Jansen, *Machine Learning for Trading*, 3rd edition (per-date rank
IC, IC by horizon, quantile spreads, turnover, break-even cost), re-implemented in numpy. Papers: Grinold & Kahn
(2000), *Active Portfolio Management*, ch. 6; Spearman (1904); Newey & West (1987); Ibragimov & Müller (2010);
Fisher (1935) and Efron & Tibshirani (1993), ch. 15, for randomisation tests; Benjamini & Hochberg (1995).

## What was already known (disclosed)

- Read before this registration: `stock_pit_universe/PREREG.md` and `RESULTS.md` (all four registered rules trail
  the equal-weight point-in-time universe over 151 months, Newey-West t from -0.41 to -0.97; every one is positive in
  the first half, to April 2020, and negative in the second), and the v1 result in `evals/stock_backtest/`.
- The sub-period boundary used here, 30-Apr-2020, is that published half boundary, chosen so the two studies can be
  compared. Family B therefore tests a difference whose sign at the portfolio level is already known: it decomposes
  a seen result and is not fresh evidence of decay.
- Not read: any price, any cached market data, any database row. No IC had been computed on these prices before this
  registration was pushed. The calibration below was run on generated arrays only.

## Data and panel

- **Prices**: the one-off harvest the base experiment read (`prices/<symbol>.csv`, `actions/<symbol>.json`), loaded
  and back-adjusted for splits and bonuses by `stock_backtest.load`. Price returns without dividends. No network and
  no database.
- **Calendar**: the trading days of NIFTYBEES; decision dates t are its month-ends; t_k is the month-end k months
  after t.
- **Membership, as of the decision date**: the Nifty 50 at t is `stock_universe.members_on` over the reconstruction
  from the pinned constituent list (`evals/stock_backtest/nifty50_constituents_2026-09-30.csv`) and the pinned change
  log (`stock_pit_universe/nifty50_changes.csv`): every change effective on or before t is applied and none after
  it. A name excluded later is still in t's cross-section for every horizon; a name included later is not in it.
- **Eligible at t**: a member with a bar on t and a known `stock_backtest.state_at` there (253 bars, no unexplained
  jump among them), the base experiment's rule.
- **Scores** use adjusted closes up to and including t, nothing later. Higher is better for every score.
- **Price at a month-end d**: the adjusted close on d, else on the last bar within 7 calendar days before d. If the
  series has no bar in that window and none after d, the stock stopped trading (delisted or merged): its price is
  its last adjusted close, i.e. the position sits in cash at 0 % from then, and the name-date is counted as `ended`.
  If the series resumes after d, the price is unknown and counted as `gap`.
- **Forward return** over (a, b] months: R(t; a, b) = P(t_b) / P(t_a) - 1, with P(t_0) the close on t. It is unknown,
  and counted as `jump`, when an unexplained jump day lies after the bar of t_a and on or before the bar of t_b. A
  date is evaluated for a window only when t_b is in the calendar.
- **A month's cross-section** for (component, window): the eligible names with a known score and a known return.
  Fewer than 20, or either side constant: that month's IC is unknown and counted, never 0.
- **Look-ahead audit**, run by the command before any statistic, on every 12th decision date and the last: each
  score recomputed from the series cut at t must equal the panel's, and each forward return recomputed after
  doubling every close before t must equal the panel's. A mismatch refuses the report.

## Components

| Key | Score at t | Where it comes from |
|---|---|---|
| `mom_score` | 12-1 momentum ÷ 252-day realised volatility | composite v1's 40-point factor; the sort of B0 |
| `trend_distance` | adjusted close ÷ its 200-day average − 1 | composite v1's 20-point factor |
| `low_vol` | minus the 252-day realised volatility | the sort of V2 |
| `mom_lowvol_rank` | minus the mean of the momentum rank and the low-volatility rank over the eligible names | the sort of V3 |
| `composite_price` | 40 × clip(mom_score ÷ 1.5, −1, 1) + 20 × clip(trend_distance ÷ 10 %, −1, 1) | the price part of composite v1 |

The other parts of composite v1 (P/E percentile, FII + DII change, promoter change, forensic flags) have no
point-in-time history in the price cache. They are reported as **unknown** with that reason and are in no family.

## Statistics

- **IC**: per month, Spearman's rank correlation of score and forward return over the cross-section (the Pearson
  correlation of the two rank vectors; tied values share the average rank).
- **Per component and horizon (1, 3, 6, 12 months)**: evaluated months, mean IC, its sample standard deviation,
  ICIR = mean ÷ sd (per month, not annualised), the share of months with IC > 0.
- **Newey-West t** (Bartlett): lags 6, 6, 12, 24 for the four horizons, i.e. max(6, 2h), at least the horizon. It is
  reported because the issue asks for it and is **never used for a p or a q**: on overlapping horizons it rejects a
  true null too often (calibration below).
- **Blocks**: the evaluated months in date order are cut into consecutive blocks of 12, 12, 18, 24 months for the four
  horizons (at least 12 and at least 2h). A remainder shorter than half a block joins the block before it.
- **Interval**: mean IC ± t(q − 1) × sd(block means) ÷ √q over the q blocks (Ibragimov & Müller 2010), at 95 %;
  unknown below 3 blocks. The minimum detectable mean IC at 80 % power, from the same standard error, is printed.
- **Decay**: the mean IC against the return over the segments (0, 1], (1, 3], (3, 6], (6, 12] months ahead, each with
  the interval of the segment's end horizon. Descriptive.
- **Sub-periods**: decision dates before 30-Apr-2020 and from it on: the same summary and interval per period, blocks
  formed inside the period, for every horizon. Descriptive, except family B.
- **Quintiles**: names ranked by score; quintile = min(5, floor(5 × (rank − 0.5) ÷ n) + 1), so tied scores share a
  quintile. Reported: the mean forward return of each quintile and of the whole cross-section, the spreads
  Q5 − Q1 and Q5 − cross-section mean (mean, Newey-West t, block interval), and a monotonicity measure (Spearman's
  correlation of 1..5 with the five mean returns, and how many of the four steps go up). A month with an empty
  quintile is unknown for the spreads and counted. The plain Q5 is not B0's rule: B0 adds the trend gate and leaves
  empty slots in cash.
- **Turnover**: for quintile k, the share of the names in it at t_h that were not in it at t (quintiles of the score
  over every eligible name, no return needed): the one-way turnover of an equal-weight quintile rebalanced every h
  months, weight drift ignored.
- **Break-even cost**: the one-way cost per side at which the mean gross spread is zero. Long-only: mean(Q5 −
  cross-section mean) ÷ (2 × mean turnover of Q5). Long-short: mean(Q5 − Q1) ÷ (2 × (turnover of Q5 + turnover of
  Q1)). Unknown, never 0, when the mean spread is not positive or the turnover is 0 or unknown. Shown beside the base
  harness's own per-side cost, (buy + sell) ÷ 2 = about 0.16 %. The benchmark's rebalancing cost is ignored, which
  lowers the break-even slightly.

## The nulls, the two families and issue #506 item 5

Item 5 asks for "a null by block permutation of dates (cross-sections kept intact), so the IC's p-value does not rest
on normality". The mean IC does not change when dates are reordered, so a date-axis randomisation has to be one of
these two, and both are registered:

- **Family A, 20 tests** (5 components × 4 horizons, full sample). H0: the mean IC is 0. Two-sided **sign-flip
  randomisation over blocks of dates**: with S_b the sum of block b's ICs, p is the share of all 2^q sign vectors e
  with |Σ e_b S_b| ≥ |Σ S_b|. Each date's cross-section, and so its IC, is kept intact; whole blocks of dates change
  sign together. Exact up to 20 blocks, else 5,000 random vectors (seed 506). Unknown below 5 blocks.
- **Family B, 10 tests** (5 components × horizons 1 and 3). H0: the two periods' block means are exchangeable (the
  same mean IC before and after 30-Apr-2020). **Whole blocks are reassigned between the periods**; p is the share of
  all reassignments whose difference of period means is at least as large in absolute value. Unknown unless each
  period has 4 blocks. Horizons 6 and 12 leave too few blocks per period for any p below 0.05, so their sub-period
  figures are descriptive only.
- **q-values**: Benjamini-Hochberg within each family separately, with the family size fixed at 20 and at 10 whatever
  the number of unknown cells (an unknown p has an unknown q and still counts). A cell is "detected" at q ≤ 0.10.
  Nothing outside the two families gets a q.

No normality is assumed for a p. The tests are valid when block sums are symmetric about zero (A) or exchangeable (B)
and independent across blocks under H0; forward windows overlap a block boundary by up to h − 1 months, which is why a
block is at least 2h months.

**Departure from the source, stated:** the book's null relabels the stocks once per block of dates. It is not used.
Relabelling breaks the link between a persistent score and market beta, so its permuted ICs vary far less than real
ones when returns share a market factor, and it rejects a true null far too often (next section).

## Calibration on a synthetic null (generated arrays only)

Recipe (`specs.CALIBRATION["recipe"]`, reproduced draw for draw by `factordiag/synth.py`): 50 names, 151 decision
months; returns r = β m + σ ε with β ~ U(0.6, 1.5), σ rising with β from 5 % to 9 % a month, the market m ~ N(0, 5.5 %)
with mean 0, so **no score has any ranking skill**. Three scores: `lowvol` (persistent and tied to β), `mom` (the
trailing 11 months' return ÷ σ) and `iid` noise. 2,000 panels, seed 506. Share of panels rejecting at a nominal 5 %:

| Procedure | 1 month | 3 months | 6 months | 12 months |
|---|---|---|---|---|
| Family A sign-flip p ≤ 0.05 (lowvol / mom / iid) | 4.6 / 4.7 / 5.1 % | 5.8 / 6.0 / 4.6 % | 4.5 / 5.6 / 5.0 % | 4.6 / 4.2 / 3.1 % |
| 95 % block interval misses 0 | 4.6 / 4.9 / 5.1 % | 6.0 / 5.8 / 4.6 % | 5.1 / 5.7 / 5.1 % | 6.4 / 5.9 / 4.8 % |
| Newey-West t beyond ±1.96 (reported only) | 6.4 / 6.1 / 6.2 % | 8.4 / 8.7 / 6.1 % | 10.5 / 10.2 / 7.8 % | 15.3 / 13.5 / 10.7 % |
| Family B p ≤ 0.05 | 5.0 / 5.7 / 4.9 % | 6.2 / 6.4 / 5.0 % | not tested | not tested |
| Relabelling of stocks (not used; 400 panels) | 21 % / 4 % / 6 % | 24 % / 6 % / 5 % | 25 % / 5 % / 6 % | 24 % / 5 % / 6 % |

Known limit of family A: at 12 months about 140 months make 6 blocks, so no p can be below 2 ÷ 2^6 = 0.031. A
12-month cell can reach q ≤ 0.10 only together with at least six other cells. The sample holds about six independent
two-year blocks at that horizon, and the report prints each cell's smallest attainable p.

## What each outcome would and would not justify

**Whatever the outcome**: `signals/stock.py`, `CALLS_ENABLED`, the card, the forecast ledger, the inventory status of
the stock composite and the base experiment's trial count stay as they are. Nothing found here is called an edge. Any
follow-up is a new variant with its own PREREG, and that PREREG must disclose this diagnostic's 30 tests as earlier
looks at the same panel.

| Outcome, per component ("detected at h" = family A's q ≤ 0.10) | Would justify | Would not justify |
|---|---|---|
| **None detected**: "no detectable ranking skill on this panel" | Leaving the component as it is; treating a re-weighting of the same price inputs on the Nifty 50 as unpromising | Saying the IC is zero (the minimum detectable IC is printed); removing it from the informational card; another look at the same panel |
| **Detected, negative mean IC**: "ranks the wrong way on this panel" | One registered test of a composite without that component, or with it reversed | Reversing or dropping it in the live composite |
| **Detected positive, long-only spread's interval not wholly above 0**: "ranking skill the top quintile does not capture" | One registered test of a different portfolio rule for that component (rank-weighted, or terciles) | Changing the live signal; any statement about B0's trend gate or cash slots, which the plain quintile does not have |
| **Detected positive, spread above 0, break-even cost below the harness's cost**: "skill smaller than its trading cost" | One registered test of a lower-turnover construction (longer holding period or buffer bands) | Any live change |
| **Detected positive, spread above 0, break-even cost at or above the harness's**: "the diagnostic and the portfolio test disagree" | An audit of what differs between the plain quintile and B0 (trend gate, cash slots, k rounding, drifted weights, benchmark costs), then one registered re-test | Adoption |
| **Detected at 1 or 3 months, not at 12, and the (6, 12] segment's interval includes 0**: "skill at short horizons only" | Registering a follow-up at a shorter holding period than the signal's 12-month event | Changing the live signal's horizon or event; reading "not detected at 12" (few blocks) as "zero at 12" |
| **Family B's q ≤ 0.10**: "the IC differs before and after April 2020" | Labelling the earlier evidence regime-specific in a follow-up's PREREG | Fitting on the better period or leaving the other out; calling it fresh evidence of decay |
| **Unknown** (too few months or blocks, or a detected cell whose spread interval or break-even cost is unknown) | Nothing | Anything |

## Known limits

- Seen data, in sample, 30 tests in two families: a diagnosis, not a validation.
- A forward return across an unexplained price jump is unknown (the base harness's rule). That drops some real moves
  (the base results name YESBANK in March 2020), it is selection on the outcome, and it grows with the horizon; the
  dropped name-dates are counted per horizon.
- `ended` names are held in cash at 0 % from their last close. For a merger the holder really received the
  acquirer's shares, so this is an approximation; the names and counts are listed.
- Price returns without dividends; decisions and prices at the same month-end close, as the base harness.
- Only the price components have a point-in-time history. The diagnosis says nothing about valuation, shareholding
  or forensic flags.
- About 50 names a month: one month's IC has a standard error near 0.14 even with no market factor, and far more in
  months when the market moves and a score is tied to beta.

## Output and the owner's one-shot command

`uv run finresearch eval factor-diagnostics` (run once by the owner, from the checkout that holds the price cache)
reads the cache and the two pinned membership files, opens no database and no network connection, and writes
`results.json` and `RESULTS.md` to this folder: statistics, counts, the monthly IC and spread series and the symbols
counted as `ended`; no price and no per-name return. It refuses to replace an existing `results.json` unless told to.
A re-run is deterministic.

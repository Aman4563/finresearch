# Pre-registration: stock backtest on a point-in-time Nifty 50 universe (S1), then S3, S4, S8

Registered 1-Oct-2026, before any backtest in this directory was run and before the price harvest finished. Issue
#147 (part of #101). Research basis: portfolio-insights research §2.2 (S1, S3, S4, S8 and the "discipline for every
stock experiment"), roadmap §D.2; Faber 2007 [42] (trend filter); Blitz & van Vliet 2007 (low volatility);
Bailey & López de Prado 2014 (deflated Sharpe ratio).

## What was already known (disclosed)

`evals/stock_backtest/results.json` (momentum-trend v1 on TODAY's Nifty 50 applied to 2014–2026): CAGR 16.98 % vs
17.51 % for the equal-weight universe, i.e. no edge over the universe; +7.8 pp over the Nifty 50 price index, which is
universe and survivorship, not skill. Nothing below is tuned on those numbers; every parameter is the v1 one or a
textbook default fixed here.

## Point-in-time membership (S1)

- Source: NSE Indices press releases (niftyindices.com/press-release, `Press_Release/ind_prs*.pdf`), the "Nifty 50" /
  "CNX Nifty" section of each semi-annual and ad-hoc replacement notice, 2014–2026. The change log with the press
  release file on every row is committed as `nifty50_changes.csv`.
- Reconstruction: start from the NSE list fetched 30-Sep-2026 (`evals/stock_backtest/nifty50_constituents_2026-09-30.csv`)
  and undo each change backwards. Invariants checked at every step: an included stock is in the later set, an
  excluded one is not, and the set has 50 names (51 while Tata Motors DVR was a member, as the 2016 notice states).
  All 25 dated changes back to 28-Mar-2014 satisfy them. Cross-check (secondary source, not used as input):
  every 2014–2025 change in Wikipedia's "NIFTY 50 → Index changes" table (read 1-Oct-2026) appears in the log.
- Membership at month-end t = the set after every change with effective date ≤ t. Two temporary spin-off
  inclusions (Jio Financial, Jul–Aug 2023; Tata Motors CV, Oct–Nov 2025) are ignored: without 252 days of history
  they could never be eligible under the rule.
- Symbol changes are mapped to the symbol NSE serves history under (e.g. ZOMATO→ETERNAL, TATAMOTORS→TMPV,
  MCDOWELL-N→UNITDSPR, IBULHSGFIN→SAMMAANCAP, INFRATEL→INDUSTOWER). A probe on 1-Oct-2026 found NSE still
  serves history for delisted members (HDFC, CAIRN, RANBAXY, IDFC, TATAMTRDVR) and under both old and new symbols;
  any member whose history is nonetheless missing is dropped; the results report
  member-months with prices / total member-months. Missing members are exactly survivorship cases, so a low ratio
  limits how "point in time" the result is.

## Common harness (unchanged from v1 unless stated)

- Monthly walk-forward, decisions at each month-end close, trades at that close; costs per side as v1 (STT 0.1 %,
  stamp 0.015 % on buys, NSE txn + GST, SEBI fee, 5 bp slippage; 0 brokerage). Price returns (no dividends).
  Cash earns 0 % (as v1; a T-bill sensitivity would be descriptive only and is not part of the bar).
- Calendar: month-ends of NIFTYBEES's NSE trading days (the NSE index endpoint has holes; NIFTYBEES does not).
- Eligible names at t: PIT members with ≥ 252 clean trading days of history at t (the v1 `state_at` rule).
- Benchmark (the bar's reference): the eligible PIT members, equal weight, rebalanced monthly, same costs.
- A stock that stops trading inside a month (merger/delisting) drops out of that month's return for both the
  strategy and the benchmark (a small, symmetric bias; stated).

## Strategies (fixed now; no other variants will be run)

- **B0, baseline (S1):** momentum-trend v1 unchanged: among eligible names above their 200-DMA, hold the top
  quintile (k = round(N/5)) by 12-1 momentum ÷ 252-day volatility, equal weight; empty slots in cash.
  Descriptive twin: B0 on today's survivor list over the same calendar, to measure the survivorship effect.
- **V1, market trend filter (S3):** B0, but only when NIFTYBEES's split-adjusted close at t is above its 200-day
  simple average; otherwise 100 % cash for the month.
- **V2, low-volatility sleeve (S4):** the lowest quintile (k = round(N/5)) of eligible names by 252-day realised
  volatility, equal weight, monthly. No trend condition.
- **V3, ensemble (S8):** rank-average of the momentum score (high = good) and the 252-day volatility (low = good)
  across eligible names, equal weights on the two ranks; hold the top quintile, equal weight. No fitting.

## Metrics

Per strategy: CAGR (net and gross of costs), annualised volatility, max drawdown, turnover, cost drag, monthly hit
rate vs the benchmark (Wilson), mean monthly excess return vs the equal-weight PIT universe, its Newey–West t
(Bartlett, 6 lags), the annualised Sharpe of excess returns, and the deflated Sharpe ratio (Bailey & López de Prado
2014) of each variant's excess returns with N = 4 trials (B0, V1, V2, V3), using the cross-trial variance of their
Sharpe ratios and each series' skewness and kurtosis. Sub-periods (first/second half) are reported.

## Pass bar (per variant V1–V3; B0 is the reference rerun)

A variant passes only if its **net excess return vs the equal-weight PIT universe is positive AND its Newey–West t
exceeds 2.39**, the two-sided Bonferroni critical value for 3 tests at α = 0.05 (z at 1 − 0.05/6). The deflated
Sharpe ratio is reported alongside (≥ 0.95 is the conventional threshold) but the t-bar decides.

## Decision rule (fixed now)

- If exactly one variant passes: change `signals/stock.py` to include that rule (V1: a market-regime factor that
  moves the action to cash-side when Nifty is below its 200-DMA; V2/V3: the corresponding factor and weights),
  version the method name ("composite v2"), and point its validation text at these results.
- If several pass: ship the first passing in the order V1, V2, V3 (simplest change first).
- If none passes: record the results; `signals/stock.py` stays "rule_based" with "no edge vs the universe"; only
  its survivorship caveat is updated to cite the point-in-time rerun.

Artefacts: `nifty50_changes.csv`, `membership_monthly.csv`, `results.json`, `RESULTS.md` in this directory, from
`uv run python -m finresearch.evals.stock_universe`.

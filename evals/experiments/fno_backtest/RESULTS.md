# Results: F&O backtest, first real-data run (post hoc to PREREG.md)

Run 12-Oct-2026 with registration hash `6d92407630d7…` (unchanged since commit ebc0925) on the read-only market-data
copy `finresearch_fnohist`: NSE F&O bhavcopy for NIFTY, BANKNIFTY, FINNIFTY and MIDCPNIFTY, 3-Jan-2022 to
9-Oct-2026 (2,538,882 contract-days; 30-day IV series and contract calendar from #306). The copy holds no stock
options, so the two overlays (covered call, protective put) could not be evaluated on real prices; their code is
covered only by the synthetic tests.

`finresearch fno backtest run --history-url postgresql+psycopg://localhost/finresearch_fnohist --json …`

**Nothing passed.** No strategy met the pre-registered out-of-sample criteria, so none is a candidate.

## Out of sample (1-Jul-2024 to 9-Oct-2026), ₹10 lakh book, after all charges

| strategy | trades | ₹/trade | t (clustered) | 95 % CI ₹/trade | win % | worst ₹ | 5 % CVaR ₹ | max DD % | Sharpe | DSR | ₹/trade, slippage ×2 | in-sample ₹/trade (t) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| NIFTY put credit spread, monthly | 47 | −1,789 | −1.30 | −4,581 … 569 | 70 | −20,877 | −17,937 | 10.8 | −1.00 | 0.00 | −1,932 | −221 (−0.17) |
| NIFTY call credit spread, monthly | 49 | 679 | 0.73 | −1,309 … 2,289 | 86 | −21,192 | −16,292 | 3.5 | 0.24 | 0.01 | 513 | −633 (−0.47) |
| BANKNIFTY iron condor, monthly | 39 | −243 | −0.18 | −3,046 … 2,068 | 72 | −19,113 | −18,374 | 5.9 | 0.10 | 0.00 | −1,050 | 1,816 (1.04) |
| NIFTY iron condor, weekly | 113 | 741 | 0.76 | −1,262 … 2,567 | 74 | −39,979 | −28,340 | 7.7 | 0.44 | 0.01 | 208 | 3,397 (2.22) |
| NIFTY debit call spread (control) | 30 | −27,775 | −3.86 | −40,674 … −13,289 | 17 | −49,516 | −49,335 | 93.8 | −2.62 | 0.00 | −26,550 | −3,614 (−0.35) |
| NIFTY debit put spread (control) | 30 | 21,492 | 1.78 | −1,977 … 44,029 | 53 | −48,358 | −46,762 | 31.8 | 1.07 | 0.09 | 18,964 | 7,390 (0.60) |
| covered call / protective put | — | — | — | — | — | — | — | — | — | — | — | no stock options in the copy |

Reading it:
- The weekly NIFTY iron condor was the only spec significant in sample (t 2.22, CI above 0), and it decayed out of
  sample to t 0.76 with a CI spanning 0; doubling slippage leaves ₹208 a trade. A typical in-sample "edge" that does
  not survive.
- The monthly premium sellers win 70-86 % of trades but one loss erases 3-5 wins (average loss ₹10.9-12.5k against
  average win ₹2.5-3.9k); none has a positive expectancy distinguishable from zero.
- Option buying (the controls) was a directional bet, not an edge: the debit call spread lost 25 of 30 OOS trades
  (−₹17k and −₹43k a trade in 2025 and 2026), the debit put spread won 16 of 30; holding both would have cost about
  ₹6.3k per pair of trades. Both carry drawdowns of 30-94 % of capital at this sizing.
- The deflated Sharpe column reads 0.00-0.09 even for a Sharpe-1.07 control because the variance of the trials'
  Sharpe ratios includes the two controls (as registered), which makes the expected best-of-8 Sharpe large. The
  sellers fail on the t-statistic and the bootstrap CI independently of it.
- POST HOC, not a finding: by IV regime, sellers did better when the 30-day IV was in the top third of its year and worse in the middle
  third (e.g. weekly condor ₹3,729 vs −₹432 a trade), but this split was not pre-registered: it is a hypothesis for
  a new, separately registered variant, not a finding.

## Post hoc engine fix (disclosed)

The first run (same hash) left 4 debit-spread trades of the NIFTY June-2023 expiry unsettled: NSE moved that expiry
from Thursday 29th to Wednesday 28th for a holiday and the bhavcopy relabels the contracts with the new date on their
last day. The engine now settles a contract on its real last session (and never enters on the last session before
expiry day). This was found after the out-of-sample figures had been seen; it is an engine change only (specs and
criteria untouched) and every verdict was the same before and after. Changes: in-sample ₹/trade BANKNIFTY monthly
condor ₹1,024 → ₹1,816 (30 → 31 trades), debit call spread −₹5,183 → −₹3,614, debit put spread ₹7,396 → ₹7,390;
out of sample only the weekly condor moved (114 → 113 trades, ₹787 → ₹741, t 0.82 → 0.76).

## Known data caveats in this run

- Pre-2024 lot sizes are inferred from open interest (#306); a few long-dated contracts carry a wrong gcd (NIFTY
  25 instead of 50 on some quarterly expiries, BANKNIFTY 5 on two). Sizing by the ₹ max-loss budget keeps the rupee
  exposure about the same, but brokerage per order is then spread over a different number of lots.
- In-sample charges use the later rates of `fincalc.charges` (see PREREG.md).
- The debit spreads are sized on the starting capital, not on the current equity, so a losing control can draw down
  more than 90 % of it (its CAGR on the window is then not meaningful).

## Engine change (21-Oct-2026, #352): final settlement at the uniform option settle

Post hoc, engine only (specs, criteria and `PREREG.md` untouched; `PREREG.md` still describes the old fallback, "median
K ± settlement price", which is no longer used). On an expiry's last session every expiring option row carries one
identical settle, the underlying's final settlement price; `backtest/pricing.settlement_price` now uses it, cross-checked
within 1 bp against the expiring future or the underlying when one exists, and returns None (unknown) when settles
differ with nothing to check them. Replay on NSE's 2022-23 legacy bhavcopies (494 sessions, NIFTY and BANKNIFTY, a
scratch database): 210 settled near expiries, 160 of them settled by the old K ± settle median at a wrong price (e.g.
NIFTY 13-Jan-2022 1,317.20 instead of 18,257.80), all 160 now at the uniform settle; none settles at
`options_intrinsic`. The library's verdicts are unaffected: no registered spec holds to expiry. Hold-to-expiry weekly
specs (Strategy Lab style, no IV filter, 2022-23) change materially: NIFTY weekly condor 76 of 103 trades, total
−₹37.7 lakh → −₹2.1 lakh; BANKNIFTY weekly condor 77 of 103, −₹39.3 lakh → −₹2.1 lakh; NIFTY weekly put credit 46 of
104, −₹18.1 lakh → −₹1.0 lakh; BANKNIFTY weekly put credit 50 of 103, −₹15.7 lakh → −₹0.9 lakh. Each changed trade
is flagged "settled at uniform option settle … (no future or underlying to check it)". Live Strategy Lab runs were
not replayed here (the live database was not read); re-run any hold-to-expiry weekly lab run from 2022-23.

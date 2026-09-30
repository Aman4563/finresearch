# Point-in-time Nifty 50 backtest: results

Generated 2026-10-01 by `finresearch.evals.stock_universe`. Pre-registration: `PREREG.md` (committed before this run).

Membership: 78 changes from NSE Indices press releases, invariant violations: 0. Price coverage: 8167 of 8167 member-months (100.0 %); missing: none.

| Strategy | Months | CAGR net | EW PIT universe | Excess vs EW | NW t | Max DD | Turnover/yr | Deflated SR | Passes |
|---|---|---|---|---|---|---|---|---|---|
| momentum-trend v1 (baseline, point in time) | 151 | +9.10 % | +12.18 % | -3.08 % | -0.97 | -31.17 % | 3.42 | 0.109 | — |
| v1 + market trend filter (S3) | 151 | +9.39 % | +12.18 % | -2.79 % | -0.96 | -22.04 % | 3.17 | 0.158 | no |
| low-volatility quintile (S4) | 151 | +11.40 % | +12.18 % | -0.78 % | -0.42 | -20.69 % | 1.56 | 0.242 | no |
| momentum + low-vol rank ensemble (S8) | 151 | +11.08 % | +12.18 % | -1.10 % | -0.41 | -28.82 % | 2.63 | 0.252 | no |
| v1 on today's list (survivorship twin, descriptive) | 151 | +20.73 % | +20.69 % | +0.04 % | 0.15 | -29.76 % | 3.41 | — | — |

Bar: excess > 0 and Newey–West t > 2.394 (Bonferroni, 3 variants). Deflated Sharpe with N = 4 trials is reported, not gated.

## Halves

| Strategy | Half | Period | Excess CAGR vs EW | NW t |
|---|---|---|---|---|
| B0 | first | 2014-01-31 → 2020-04-30 | +1.23 % | 0.13 |
| B0 | second | 2020-04-30 → 2026-08-31 | -7.79 % | -1.88 |
| V1 | first | 2014-01-31 → 2020-04-30 | +2.48 % | 0.24 |
| V1 | second | 2020-04-30 → 2026-08-31 | -8.50 % | -1.69 |
| V2 | first | 2014-01-31 → 2020-04-30 | +8.59 % | 1.77 |
| V2 | second | 2020-04-30 → 2026-08-31 | -10.58 % | -3.19 |
| V3 | first | 2014-01-31 → 2020-04-30 | +4.35 % | 0.82 |
| V3 | second | 2020-04-30 → 2026-08-31 | -6.99 % | -1.81 |
| B0_survivors | first | 2014-01-31 → 2020-04-30 | +2.54 % | 0.61 |
| B0_survivors | second | 2020-04-30 → 2026-08-31 | -2.67 % | -0.46 |

## Limits

- Eligible member-months (≥ 252 clean days of history at the month end): 7405 of 8167 (90.7 %).
- NIFTYBEES (the market-filter series and the price reference) CAGR over the same months: +12.60 %.
- A price jump with no matching split/bonus in NSE's corporate actions is treated as a data error: returns across it are dropped for 252 days (v1's rule). That also drops some REAL moves, e.g. YESBANK in March 2020, from the strategy and the benchmark alike; it flatters the equal-weight universe slightly. Symbols affected: ADANIENT (1), ASIANPAINT (1), AUROPHARMA (1), BEL (2), BPCL (1), HCLTECH (2), IDEA (4), IDFC (1), INDUSINDBK (1), INFY (2), IOC (2), JPASSOCIAT (1), PNB (1), SBIN (1), SUNPHARMA (1), TCS (1), TECHM (1), TMPV (1), VEDL (1), WIPRO (1), YESBANK (4), ZEEL (1).
- Price returns without dividends; cash at 0 %; a monthly close-to-close trade with 5 bp slippage.
- The survivorship twin (20.7 % CAGR) differs from the committed v1 result (17.0 %, evals/stock_backtest) because the calendar differs (NIFTYBEES month-ends from Jan-2014 here; the NSE index series, which has holes, there), not because the rule changed.
- Delisted members (HDFC, CAIRN, RANBAXY, IDFC, TATAMTRDVR, JPASSOCIAT) drop out in the month their trading stops (their last partial month is not counted).

## Decision

No variant passed the bar: signals/stock.py stays rule_based with no edge vs the universe; only its survivorship caveat now cites this point-in-time rerun.

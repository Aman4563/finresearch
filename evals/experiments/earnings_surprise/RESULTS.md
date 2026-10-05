# Earnings surprise (SUE) and post-announcement drift: results

Generated 2026-10-05 by `finresearch.evals.earnings_surprise`. Pre-registration: `PREREG.md`; changes made before any outcome was computed: `ADDENDUM.md` (re-uploaded broadcast dates; NIFTYBEES as the benchmark).

**Verdict: does not pass.** Primary test fails; tradable test fails. SUE stays informational ("experimental — not part of the signal"); signals/stock.py is unchanged.

## Coverage

- Stocks harvested: 61 of 61 point-in-time Nifty 50 members since Apr-2021 (not harvested: none). Fewer than the 13 quarters a SUE needs (quarters read): HDFCLIFE 6, JIOFIN 12, LTIM 0, NESTLEIND 8, SBILIFE 6.
- Events with an EPS SUE: 801 from 56 stocks over 19 seasons (2021-09-30 to 2026-03-31); with a complete [+2,+60] window: 799.
- Candidates dropped: no EPS SUE: no figure for the same quarter a year earlier: 32; no EPS SUE: only 0 of 8 past seasonal differences: 4; no EPS SUE: only 1 of 8 past seasonal differences: 2; no EPS SUE: only 2 of 8 past seasonal differences: 3; no EPS SUE: only 3 of 8 past seasonal differences: 4; no EPS SUE: only 4 of 8 past seasonal differences: 7; no EPS SUE: only 5 of 8 past seasonal differences: 30; not a Nifty 50 member at t0: 171.

## Tests (bar: t > 1.96)

| Test | Estimate | Clustered SE | t | Events | Months | Passes |
|---|---|---|---|---|---|---|
| Primary: drift [+2,+60] D10 - D1 (gross) | +0.39 pp | +1.21 pp | 0.32 | 159 | 39 | no |
| Tradable: long top decile, net of round trip | +0.16 pp | +1.28 pp | 0.13 | 67 | 24 | no |

Round trip cost: 0.322 % (evals.stock_backtest COST_BUY + COST_SELL). Tradable trades: 67; mean net by half: first -2.22 pp, second +1.25 pp.

## Descriptive (not gated)

| Half | Seasons | D10 - D1 drift | t |
|---|---|---|---|
| first | 2021-09-30 to 2023-09-30 | -3.02 pp | -1.64 |
| second | 2023-12-31 to 2026-03-31 | +2.99 pp | 2.18 |

- [0,+1] reaction D10 - D1: +0.49 pp (t 0.76).
- Revenue SUE, drift D10 - D1: -1.31 pp (t -0.67).

| SUE decile | Events | Mean SUE | Mean [0,+1] | Mean [+2,+60] |
|---|---|---|---|---|
| 1 | 89 | -1.83 | -0.82 pp | +0.60 pp |
| 2 | 77 | -0.55 | -0.26 pp | +2.46 pp |
| 3 | 83 | -0.09 | -0.97 pp | +1.72 pp |
| 4 | 78 | +0.19 | -0.14 pp | +0.92 pp |
| 5 | 79 | +0.42 | +0.02 pp | +1.92 pp |
| 6 | 81 | +0.74 | -0.10 pp | +2.54 pp |
| 7 | 83 | +1.15 | +0.69 pp | +0.21 pp |
| 8 | 78 | +1.57 | -0.53 pp | +0.69 pp |
| 9 | 82 | +2.29 | +1.19 pp | -1.04 pp |
| 10 | 71 | +5.04 | -0.32 pp | +0.99 pp |

## Reading (written after the run; post hoc, not a test)

- A null at this power: the clustered SE of D10 - D1 is about 1.2 pp, so a drift of 2-3 pp could exist and not be detected. The two halves point in opposite directions (-3.0 pp, t -1.64; +3.0 pp, t 2.18); the second half alone is not a pre-registered test and one half of two clearing 1.96 is expected by chance about one time in ten.
- The decile means are not monotonic in SUE for either window, and the [0,+1] reaction spread is small (+0.5 pp): for Nifty 50 names the seasonal random walk is a weak proxy for the news the market trades on (no consensus data).

## Limits

- Universe: the point-in-time Nifty 50 only (large caps, about 50 events a season, so a decile is about five stocks). Post-earnings drift is usually reported to be stronger in small caps; NIFTY 500 was not harvested.
- History: NSE serves results XBRL from about the Sep-2018 quarter (older filings are HTML and were not parsed), so the sample is about 19 seasons and the test has low power (PREREG: minimum detectable effect ≈ 4-6 pp).
- SUE is a seasonal random walk on basic EPS as first reported; no analyst consensus is available. One-off items (exceptional gains, impairments) are inside EPS and count as surprise.
- Announcement time is NSE's broadcast time. A re-upload past the legal deadline is detected and dropped (ADDENDUM 1); one inside the deadline is not detectable and would make t0 late.
- Returns are market-adjusted (no beta) price returns; dividends are excluded from stocks while NIFTYBEES tracks the index's total return (ADDENDUM 2), a ≈ 0.3 pp drag on every 60-session window that cancels in D10 - D1.
- Standard errors are clustered by the calendar month of t0; windows that overlap across months are only partly covered by that clustering.

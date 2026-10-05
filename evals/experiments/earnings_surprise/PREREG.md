# Pre-registration: earnings surprise (SUE) and post-announcement drift on the point-in-time Nifty 50

Registered 5-Oct-2026 (issue #181, research top-5 item ★4), before any results filing or price for this experiment
was harvested and before any outcome was computed. Nothing below will be changed after the harvest starts; anything
added later goes in an ADDENDUM.md and is labelled as post hoc.

## Hypothesis

Prices under-react to earnings news (post-earnings-announcement drift). Bernard & Thomas 1990, *Evidence that stock
prices do not fully reflect the implications of current earnings for future earnings*, J. Accounting & Economics
13(4) 305-340 [V: RePEc ideas.repec.org/a/eee/jaecon/v13y1990i4p305-340.html]; Foster, Olsen & Shevlin 1984,
*Earnings releases, anomalies, and the behavior of security returns*, The Accounting Review 59(4) [U: not re-read in
this session]. Neither paper's exact model was re-read here; the measure below is defined operationally.

**H1:** the mean abnormal return over trading days [+2, +60] after the announcement of the top SUE decile minus that
of the bottom SUE decile is > 0.

## Surprise measure (no consensus data exists for free in India)

- Seasonal random walk: expected EPS_q = EPS_{q-4}. Difference d_q = EPS_q - EPS_{q-4}.
- SUE_q = d_q / s, where s = the sample standard deviation (n - 1) of d over the 8 quarters before q (q-1 ... q-8).
  At least 6 of those 8 differences must exist, else no SUE. s = 0 -> no SUE. No drift term.
- EPS = basic EPS for the quarter (the XBRL's current-quarter context), rupees per share, **as first reported**:
  the earliest-broadcast filing for that quarter end, never a later revision.
- One basis per stock: consolidated if the company filed consolidated results for the majority of the harvested
  quarters, else standalone. A quarter missing on that basis is a gap; SUE needs q and q-4 on that basis.
- Splits and bonuses: each earlier EPS is put on the share basis of quarter q by dividing it by the product of the
  split/bonus factors (fincalc.signals.action_factor) whose ex-date falls after that earlier filing's broadcast and
  on or before q's broadcast.
- Revenue surprise: the same formula on quarterly revenue (revenue from operations; interest earned for banks).
  Descriptive only, not gated.

## Point in time

- Announcement time = the earliest NSE broadcast timestamp (IST) of any filing (either basis) for that quarter end,
  from NSE's Financial Results index (`broadCastDate`) or Integrated Filing index (`broadcast_Date` of the Original).
- t0 = that day if it is a trading session and the broadcast is before 15:30 IST; otherwise the next session.
  A timestamp with no time of day counts as after the close.
- Trading calendar = the union of the dates on which any harvested stock traded (NSE sessions; every session has
  trades in a 60-stock large-cap set). Holidays and weekends are therefore skipped.
- SUE at q uses only filings broadcast at or before q's own broadcast.

## Returns

- Benchmark: the NIFTY 50 price index (NSE `indicesHistory`). Stock closes are NSE EQ series, back-adjusted for
  splits/bonuses (fincalc.signals.adjust_for_actions). Price returns (no dividends) on both sides.
- Reaction [0,+1] = P(t0+1)/P(t0-1) - I(t0+1)/I(t0-1). Drift [+2,+60] = P(t0+60)/P(t0+1) - I(t0+60)/I(t0+1).
  Market-adjusted (no beta is estimated). An event whose window lacks a needed stock or index close, or contains an
  unexplained ±35 % one-day move (adjust_for_actions anomalies), is dropped and counted.

## Universe and sample

- Point-in-time Nifty 50: an event counts only if the stock was a member at the close of t0 (membership from
  `evals/experiments/stock_pit_universe/nifty50_changes.csv`, 78 cited changes; 61 symbols were members at some time
  since Apr-2021). Survivorship caveat: a member whose filings or prices cannot be harvested drops out; coverage is
  reported. NIFTY 500 is not in this run (≈ 500 x 60 requests is beyond the polite budget).
- Filings with XBRL exist on NSE from about the Sep-2018 quarter (older ones are HTML; not parsed), so with 4 + 8
  quarters of history the first SUE is about the Sep-2021 quarter. Sample: quarters ending Sep-2021 to Mar-2026 (19
  seasons); the Jun-2026 season is excluded because its +60 window is not complete.
- Expected N ≈ 900 events, ≈ 90 per extreme decile. With a per-event drift s.d. of ~10 %, the standard error of the
  D10 - D1 difference is ≈ 1.5-2 pp before clustering, so the minimum detectable effect at 80 % power is ≈ 4-6 pp.
  A null result is therefore weak evidence of no effect, and will be reported as such.

## Test (fixed; no other variants)

1. **Primary (H1, academic):** deciles of SUE formed within each season (all events of the same quarter end).
   Statistic: mean drift(D10) - mean drift(D1), gross; standard error clustered by the calendar month of t0
   (the estimator's influence summed per cluster, G/(G-1) correction). Pass: difference > 0 and t > 1.96.
2. **Tradable (walk-forward, after costs):** at each event, the breakpoint is the 90th percentile of all SUEs with
   t0 in the 365 days before this t0 (strictly earlier; at least 100 of them, else no trade). Long-only: buy at the
   close of t0+1 when SUE ≥ the breakpoint, sell at the close of t0+60. Net excess = drift - round-trip cost.
   Pass: mean net excess > 0 and clustered t > 1.96, AND mean net excess > 0 in each half of the seasons.
   (Short selling a stock for 60 days is not available to a retail cash-market investor, so no short leg.)
- Costs: `evals.stock_backtest.COST_BUY + COST_SELL` (STT 0.1 % each side, stamp 0.015 % on the buy, NSE
  transaction charge + GST, SEBI fee, 5 bp slippage each side; ≈ 0.32 % round trip; brokerage 0).
  `fincalc/charges.py` covers only F&O legs, so it is not used.
- Reported descriptively (not gated): D10-D1 per half, mean drift by decile, the [0,+1] reaction by decile,
  revenue-SUE D10-D1, event coverage.

## Decision rule

- Both the primary and the tradable tests pass: SUE may be added to `signals/stock.py` as a factor (a separate PR,
  versioned method name, validation text pointing at RESULTS.md).
- Otherwise: SUE stays informational. The stock page shows the latest SUE, its decile and the [0,+1] reaction labelled
  "experimental — not part of the signal", and signals/stock.py is unchanged.

Artefacts: `RESULTS.md` and `results.json` in this directory, from
`uv run python -m finresearch.evals.earnings_surprise`; the harvest is `finresearch.evals.earnings_harvest`.

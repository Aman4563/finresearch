# Pre-registration: the post-trade review's before/after look (P2.8, #383), version `post-trade-review-v1`

Registered 22-Oct-2026, before the review page existed and before any of the three measures below was computed on
the owner's trades: no paper trade, card log or mark row was read for it. It is never edited: changes go to
`ADDENDUM.md` as post hoc.

This is a single-subject, descriptive before/after comparison. It makes no causal claim: one person, no control,
and anything else that changed in the same months (markets, the app, the owner) is confounded with the page.

## Split

- **Day 0** = the IST day the `/fno/review` page is first deployed to the live app (recorded in `ADDENDUM.md` the day
  it happens, from `merge_deploy.sh`'s log).
- **Before** = the 3 calendar months ending the day before day 0; **after** = the 3 calendar months starting day 0.
  The after window is read once, after it ends; no interim look.

## Measures (paper trades; real trades join only if their fills become readable, recorded in the addendum first)

1. **Adherence rate**: `fincalc.posttrade.adherence(...).rate` over the trades closed in the window (planned exits ÷
   trades with a plan; trades with no plan left out; the 24 h window of `PLANNED_WINDOW_HOURS`).
2. **IS per trade**: the mean of `fnobook.review.shortfall_of(...)["total"]` (₹) over the trades opened in the window
   whose shortfall is known; the share known is reported beside it.
3. **Same-day-expiry share**: `fincalc.posttrade.exposure_mix(...).same_day_expiry_share` over the trades opened in
   the window.

## Reporting

- Each measure before and after, the after − before difference, and a 95 % percentile bootstrap CI of that difference
  (10,000 resamples of trades with replacement within each window, seed 383).
- With fewer than 10 trades in either window for a measure, that measure reads "insufficient data" (no CI).
- Unknown values are excluded and counted, never filled with 0.
- No threshold, no verdict, no promotion: the numbers are reported descriptively for the owner.

## Deferred (MF16)

The disposition effect (DE = PGR − PLR, Odean 1998) and loss-chasing metrics start only at 100 closed trades, with
unit tests on synthetic trade logs with golden PGR and PLR values first. Not part of this registration.

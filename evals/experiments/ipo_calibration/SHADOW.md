# Pre-registration: the live shadow test of the E-IPO-1 blend (switch criterion)

Registered 1-Oct-2026. That is before any live outcome of the blend exists: the first shadow forecast, ledger row 253
(ORIENTCABL, logged 1-Oct-2026 01:07 IST), resolves on its listing day, expected 5-Oct-2026. Coordinator decision on
PR #151, issue #147.

## Why shadow and not live

The blend passed the walk-forward bar at the minimum. It won 5 of 7 years, and 2022 by only +0.002. It passed only
after a post-hoc input fix (`ADDENDUM.md`), and it was one of three variants tried against an uncorrected bar. The
live IPO call therefore stays the empirical base-rate table: method `BASE_RATE_METHOD`, validation `base_rate`.

The blend is computed alongside the call and logged in the forecast ledger as its own method (`BLEND_METHOD`, "shrinkage
blend v1 …") with validation status `shadow`. It is shown on the IPO card only as an "experimental comparison". It
never sets the action, the sizing or the rules metric `p_listing_gain`.

## Data the criterion uses

- **Pairs.** A pair is one mainboard IPO for which BOTH methods logged a forecast of the same event ("NSE listing-day
  open above the issue price") before the listing, and both forecasts resolved (not void).
- **Which forecast counts.** For each method, the issue's latest forecast before resolution counts. The ledger keeps
  one row per method per IST day; among days, the latest day wins.
- **Which rows count.** Every blend-method row is a shadow row: row 253 above, or rows logged after this file.
  Research-run verdicts (`report verdict, fixed confidence map v1`) are not part of the comparison.

## Criterion (fixed now)

- **When it is evaluated.** First evaluated when n ≥ 30 pairs, then again at every 10 further pairs. Every evaluation
  is reported, including the ones that do not switch.
- **Statistic.** d_i = (p_table,i − y_i)² − (p_blend,i − y_i)²; positive means the blend was better on issue i.
  Mean d̄ = Brier(table) − Brier(blend).
- **Interval.** Paired bootstrap: 10,000 resamples of the n pairs with replacement, seed 20261001, and the 5th and
  95th percentiles of the resampled means, which form a 90 % interval.
- **Switch rule.** Switch the live call to the blend only if d̄ > 0 AND the lower end of that 90 % interval is > 0.
  Otherwise keep the table.
- **After a switch.** The switch is a reviewed code change. Its validation text would cite the live result (n, d̄,
  interval) alongside the walk-forward.
- **Stopping for failure.** If by n = 60 the rule has not fired, the shadow is retired: stop logging it, and keep
  the rows. This caps repeated looks at four (30, 40, 50, 60). The look count is not corrected for; it is disclosed
  here instead.

The computation is `finresearch.evals.ipo_shadow.shadow_verdict`, committed with this file together with its tests.

## What does not change the criterion

- **Harvest refreshes.** Refreshing the IPO history or refitting λ does not reset the count. A refit changes the
  method's inputs, not its name, and the pairs keep accruing.
- **Features the blend cannot compute.** If a live feature is missing (OFS share unparseable, Nifty history
  unavailable), the imputation is logged in the shadow's caveats. The pair still counts.

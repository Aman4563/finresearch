# Pre-registration: the variance risk premium in the form this trader could collect (P3.6, #389)

Registered 22-Oct-2026, before any study code that can read a stored session existed: no `fno_bhav`, `fno_contract` or
`index_eod` row has been queried for this study, and the 2008-2021 vault has not been read for it. The registration
is `src/finresearch/vrp/specs.py` (`vrp-collectable-v1`); its sha256
`73b3e015921e2b636c32b5d5b54f92a7baa53c390d2a26d15fe8c4cba1737aad` is pinned in `vrp/prereg.py`, and the study refuses
to run while the two differ. Every numeric setting is read from the module that uses it (`fincalc/mfiv.py`,
`vrp/measure.py`, `vrp/study.py`, `vrp/collect.py`, `vrp/run.py`, `fincalc/fno_history.py`, `fincalc/realvol.py`, the
backtest's `Book` and criteria v2 carry, `fincalc/margin_approx.py`), so editing one moves the hash. This file restates
it in prose; where they differ, `specs.py` wins. It is never edited: changes go to `ADDENDUM.md` as post hoc, with a
new `SPEC_VERSION` and hash committed before the changed study reads a stored session.

This study implements the vault study `vrp_primary` (`evals/experiments/vault/vrp.md`, sha256
`a9aa38e8e676cdf4ba14ddfe13916eb0dbb88696781dc1f3640d174427719be5`, sealed in the vault pack #364) without editing it,
and fixes what that file leaves open. It searches no strategy, adds nothing to `VARIANTS_TRIED`, changes no card and
does not change the evidence tag. The outcome is reported whatever it is and nothing is adopted.

## Measures

- **sigma^2_MF,t** (Cboe v6.0 section 3; NSE India VIX white paper sections 3-6, both fetched 22-Oct-2026 and both
  worked examples reproduced in `tests/test_mfiv.py`: Cboe's sigma^2 = 0.019233906 and 0.019423884 and VIX 13.93 from
  the raw quotes; India VIX's 0.072979 and 0.070942). Per term: F = the same-expiry future's settle (India VIX), else
  Cboe's parity forward; K0 = the strike at or below F, priced as the mean of its call and put; OTM puts below and
  calls above, skipping a strike without a market price and stopping at the second consecutive one (Cboe's zero-bid
  rule; a "market price" is `fno_history.market_settle`, the bhavcopy having no bids); R = `fno_history.RATE` (6.5 %;
  India VIX uses NSE MIBOR, no dated series is stored [U]). Unknown with the reason when fewer than 3 strikes per side
  (India VIX's 3-knot minimum) or a gap above 5 % of F between neighbouring strip strikes [U: choice].
- **Terms and the 30-day value:** near and next monthly expiries (the latest listed expiry of each month), rolled to
  next and far with 3 or fewer sessions left (India VIX); T = minutes from the close to the expiry's close / 525,600;
  combined in minutes as both papers do. At a monthly expiry's close both remaining monthlies are often beyond 30
  days, so the formula extrapolates (negative far weight): intended, tested.
- **Common range:** the registered primary uses band = the common moneyness range |ln K/F| that `vault_data_audit`
  check 8 fixes (covered on >= 90 % of days in every era); the strip must reach both ends and is cut to them. The
  owner passes it from the audit's stored output. The vault read is refused without it; a recent-window run without
  it is labelled "full traded strip, not era-comparable". The flat-IV extension beyond the strip (#389's single-day
  figure) is a reported sensitivity only.
- **RV(t, t + 30 cd):** n x the Yang-Zhang per-session variance (alpha 1.34) of the n sessions in (t, t + 30 cd] of
  the within-contract front future, x 365/30, so both sides are calendar-day annual variance. An invalid bar or data
  ending before t + 30 cd: that window is unknown and counted. 5-minute RV is not built here (out of scope).

## Tests (as vrp.md, made precise)

- **(0) Gate**, per era (2008-2015, 2016-2021, 2022-2026), on days with an India VIX close: correlation of daily
  changes of 100 sqrt(sigma^2_MF) and India VIX >= 0.9 AND |median(own - VIX)| < 1 vol point, on >= 20
  consecutive-session changes (fewer: unknown, which fails). Failing labels (1) and (2) "measure unvalidated".
- **(1) Primary:** windows start at each monthly expiry that is a stored session (28-day Februaries overlap the next
  window by up to 2 days; Newey-West lag 1 absorbs it). VRP_t = (sigma^2_MF,t - RV) / sigma^2_MF,t: mean, NW t (lag 1),
  normal 95 % CI, share negative, per period (vault 2008-2021, seen 2022-2026). Secondary, labelled "positive under a
  zero premium by about half Var(ln RV)": mean ln(sigma^2_MF / RV). The one vault conditional statistic: the payoff by
  the tercile of sigma^2_MF among all earlier window starts, 12-window burn-in, descriptive.
- **(2) Collectable:** 1 lot, sold at each monthly expiry's settle, cash-settled at the next monthly expiry's final
  settlement price (`backtest.pricing.settlement_price`, #352). Short ATM straddle (strike nearest the same-expiry
  future with both a call and a put market settle; ties the lower), un-hedged / band-hedged (whole futures lots of the
  same expiry, re-set to round(Delta) at a settle only when |H - Delta| >= 0.5 lot; Delta from one Black-76 IV implied
  from the out-of-the-money leg's settle) / fractional (benchmark, labelled "not collectable at 1-2 lots"); and the
  un-hedged short 25-delta put. Costs: `fincalc.charges` dated rates per trade date (unknown rate: the window is
  unknown), Rs 20 per order; option slippage from the registered tiers; futures slippage 1 bp of the price per unit
  [U]; no charge on cash settlement [U for the expiring future]. Re-run with the 1-Apr-2026 rates (STT 0.15 % / 0.05 %,
  "unconfirmed") as the collectable-today case. Reported: mean net, expiry-clustered BCa 95 % CI (5,000 draws, seed
  389), share negative, the summed losses of windows starting Sep-2008 to Mar-2009 and Feb-Jun 2020, and the margin
  view (SPAN-style estimate of the option legs at entry, the carry its cash half forgoes at 6.5 %, return on margin).
- **Near-expiry z:** at each window start, z = ln(final / F_t) / sqrt(v T) for the near monthly, v = its sigma^2_MF
  or its ATM IV^2: E[z^2] beside E|z| / sqrt(2/pi).
- **Lots:** `backtest.dbhistory.DbHistory.lot` (vault days: NSE's dated circulars only). None: the window is unknown.

## Windows and access

| window | rows | access |
| --- | --- | --- |
| seen | 1-Jan-2022 to 31-Dec-2026 | the app database (or the stored F&O history); read many times before, labelled seen |
| vault | 1-Jan-2008 to 31-Dec-2021 | ONE logged read through `vault.guard.vault_read("vrp_primary", ...)` after `vault_data_audit`, covering NIFTY and (from the audit's first qualifying year) BANKNIFTY in the same read; aggregates only are printed and written |

NIFTY is primary; BANKNIFTY is reported only, and on the vault only from the first year with >= 10 valid OTM strikes
per side on >= 90 % of days (audit check 8; the read refuses BANKNIFTY without that year). Power, stated up front:
about 57 monthly windows in 2022-26 and about 168 in the vault.

**Charges gap, found at registration (22-Oct-2026, from the code, no market data read):** `fincalc.charges.order_costs`
needs every key's rate on the trade date, and the dated table has no exchange option rate before 2017 and no stamp
duty rows before 2021. With today's table every vault window of (2) would be unknown. Because the vault read is
spent once, the run refuses the vault read while any month-end of 2008-2021 has an unknown charge key; the owner
either completes the table (#365) or records in `ADDENDUM.md`, before the read, that (2) is read as unknown.

## Reading rule

Nothing is adopted. A VRP-conditioned seller variant may be registered (P3.7) only if the after-cost P&L of the form
it would trade is positive in BOTH periods (vrp.md); a positive log ratio, vol-unit gap or share of IV > RV never
qualifies. The evidence tag's update (#389 "Use") is left to the owner.

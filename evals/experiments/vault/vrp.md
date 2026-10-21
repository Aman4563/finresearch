# Variance risk premium on the vault (study `vrp_primary`; P3.6)

Part of the vault pack (PREREG.md, #364). One logged read. Definitions follow #389 as revised by T6 and T16.

## Measures

- **sigma^2_MF,t:** the app's own model-free 30-day implied variance (CBOE / India VIX method) from OTM market-settled
  prices: sigma^2 = (2/T) sum_i (dK_i / K_i^2) e^(rT) Q(K_i) - (1/T)(F/K0 - 1)^2, dK_i = (K_i+1 - K_i-1)/2, on the
  near and next monthly expiries, interpolated to 30 calendar days in minutes. Eras are compared on the common
  moneyness range covered on >= 90 % of days in every era (fixed from P1.3's coverage audit before this read).
- **RV(t, t+30 calendar days):** Yang-Zhang on within-contract front-future OHLC, calendar-day variance, overnight
  returns taken within one contract on roll days. 5-minute RV (2020 on) is a sensitivity.

## (0) Gate

On days with an India VIX close (P1.4): own model-free 30-day vol vs India VIX. Pass iff the correlation of daily
changes >= 0.9 AND the median level gap < 1 vol point, by era. Vault years without India VIX are reported as "not
VIX-checked". If the gate fails, (1) and (2) are still reported but labelled "measure unvalidated".

## (1) Primary

Mean variance-swap payoff VRP_t = (sigma^2_MF,t - RV(t, t+30 cd)) / sigma^2_MF,t on non-overlapping windows that
start at each monthly expiry, Newey-West t (lag 1). Separately for 2008-2021 (vault) and 2022-26 (seen). NIFTY
primary; BANKNIFTY only from the first vault year with >= 10 valid OTM strikes per side on >= 90 % of days. Reported:
mean, NW t, 95 % CI, share of negative windows. Secondary, labelled "positive under a zero premium by about half
Var(ln RV)": mean ln(sigma^2_MF / RV).

## (2) Secondary

Short ATM straddle, 1 lot, held from each monthly expiry to the next, after all costs (P1.3's dated charges, and
re-run at the 2026 rates as the collectable-today case): un-hedged vs integer-lot band-hedged (whole futures lots
traded at the settle when |net delta| >= 0.5 lot). Expiry-clustered bootstrap CIs (5,000 draws, seed 389), share of
negative months, losses in Sep-2008 to Mar-2009 and Feb-Jun 2020. The fractional daily hedge is reported as the
academic benchmark, labelled "not collectable at 1-2 lots". Also the un-hedged short 25-delta put.

## The one conditional statistic registered on the vault

The primary payoff split by the tercile of sigma^2_MF,t within its own expanding window (terciles from all prior
window starts, point-in-time; the first 12 windows are burn-in), mean and NW t per tercile. Descriptive only, no
threshold, no adoption. No other conditional statistic is computed on the vault.

## Prerequisite for any VRP-conditioned seller variant

A seller variant conditioned on the VRP may run (`vol_race.md` §4) only if the after-cost P&L of the form it would
trade is positive in BOTH periods. Operationally: the base spec's expectancy per trade after costs is > 0 in the
`library_v1_same_print` vault read AND in the stored 2022-26 full window (published runs). A positive log ratio,
vol-unit gap or share of IV > RV never qualifies.

## Known limits

No VIX cross-check before 2013-2016; BANKNIFTY options nearly untraded in 2008; early wings are thin and the
truncation at the traded range understates sigma^2_MF.

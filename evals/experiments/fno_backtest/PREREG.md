# Pre-registration: F&O strategy backtest on NSE's bhavcopy history (issue #307)

Registered 12-Oct-2026, before any backtest was run on real data. The market-data copy of the F&O history
(`finresearch_fnohist`) was not yet available to this branch: no in-sample or out-of-sample figure on real prices had
been computed. The registration is `src/finresearch/backtest/specs.py` (specs, book, slippage model, pass criteria,
windows); its SHA-256 (`specs.registration_hash()`) is recorded in `src/finresearch/backtest/prereg.py`, and the
evaluator refuses an out-of-sample run whose hash differs. Nothing below changes after the first real run; anything
added later goes in an ADDENDUM.md and is labelled post hoc.

## Question

Does any small, pre-specified set of retail F&O strategies make money **after real costs and realistic fills** on
NSE's 2022–2026 history, out of sample? Prior: mostly no. SEBI's FY26 study found 87.7 % of individual derivative
traders lost money; only mainly-option sellers (about 2 % of traders) had a positive median
([SEBI press release 50/2026](https://www.sebi.gov.in/media-and-notifications/press-releases/aug-2026/sebi-studies-indicate-key-trends-in-retail-participation-trading-behaviour-and-profitability-in-the-equity-derivatives_103838.html)).

## Variants (8; VARIANTS_TRIED = 8)

| key | strategy | role |
|---|---|---|
| nifty_pcs_m | NIFTY put credit spread, monthly, 30–45 DTE, short ≈20 Δ, wing 2 % of the forward | candidate |
| nifty_ccs_m | NIFTY call credit spread, the same | candidate |
| banknifty_ic_m | BANKNIFTY iron condor, monthly, 30–45 DTE, shorts ≈16 Δ, wings 2.5 % | candidate |
| nifty_ic_w | NIFTY iron condor on the nearest (weekly) expiry, 4–9 DTE, shorts ≈15 Δ, wings 1 % | candidate |
| nifty_dcs_m | NIFTY debit call spread, ≈50 Δ long, 2 % wide, held to expiry | control |
| nifty_dps_m | NIFTY debit put spread, the same | control |
| stock_cc_m | covered call on a held stock: short ≈25 Δ call, 20–35 DTE, take 50 % | overlay |
| stock_pp_m | protective put on a held stock: long ≈25 Δ put, 20–35 DTE | overlay |

Sellers: take profit at 50 % of the credit, stop at a loss of 2× the credit, exit at 7 DTE (monthly) or on the last
session before expiry day (weekly), only when the 30-day IV is at or above its 30th percentile of the past year
(unknown IV = no entry). At most 2 open positions (weekly: 1), 2 per expiry, 5 days between entries. Sizing: lots =
⌊5 % of ₹10 lakh ÷ (defined max loss + entry costs) per lot⌋, capped at 20 lots and by the approximate margin of
open positions staying within the capital. Every value is in specs.py; none was fitted.

## Windows

In-sample 1-Jan-2022 to 30-Jun-2024; out-of-sample 1-Jul-2024 to the latest stored day. One continuous replay;
a trade belongs to the window of its entry day; equity statistics use the window's calendar days (so OOS equity on
1-Jul-2024 carries the marked P&L of positions opened in-sample). No parameter is fitted, so the yearly
walk-forward is the same frozen spec judged year by year. The IV percentile needs 60 sessions of history: the
history starts in January 2022, so IV-filtered sellers cannot enter before about April 2022, and their percentile
window is shorter than a year through 2022.

## Pass criteria (all must hold out of sample → "candidate"; never "guaranteed")

1. ≥ 30 OOS trades.
2. Expectancy per trade after costs > 0, with an expiry-clustered t-statistic ≥ 2 **and** a 95 % expiry-cluster
   bootstrap CI (5,000 draws, seed 307) above 0. (Both, not either.)
3. OOS maximum drawdown of the daily marked equity ≤ 20 % of capital.
4. Deflated Sharpe ratio ≥ 0.95 (Bailey & López de Prado 2014, *J. Portfolio Management* 40(5) 94–107,
   doi:10.3905/jpm.2014.40.5.094) with N = 8 and V = the variance of the OOS daily Sharpe ratios of every evaluated
   variant, controls included. With ~560 OOS days this needs an annualised Sharpe of roughly 1.2 or more: strict on
   purpose.
5. In-sample expectancy > 0 (the sign must agree).
6. OOS expectancy still > 0 with slippage doubled.

Overlays trade at most once a month (≤ ~27 OOS trades), so they cannot meet criterion 1: they are **reported** with
their evidence (and the stock-alone vs stock-with-overlay comparison), never made candidates by this run.

## Stated before the results: known limits

- **Costs before Oct-2024 use later rates.** `fincalc.charges.RATES` has STT rows from 1-Oct-2024 and exchange,
  SEBI, stamp and GST rows from 30-Sep-2026; `rates_as_of` applies the earliest row to earlier dates. In-sample
  costs therefore use those rates, not the ones in force in 2022–24 (not re-read from primary sources here; a
  decision for the owner).
- **Slippage is an assumption.** The bhavcopy has no bid/ask: per leg, 1 / 2 / 4 / 8 % of the price for ≥ 1,000 /
  100 / 10 / 1 contracts traded that day, 15 % for an untraded contract (settlement price), at least ₹0.10. The ×2
  stress run is the sensitivity.
- **Fills at the close.** Reference = the last traded price of a contract that traded that day, else NSE's
  settlement price. The leg chosen by delta must have traded ≥ 10 contracts; the wing must have traded.
- **Margin is approximate** (`fincalc.margin_approx`, a SPAN-style estimate with assumed scan ranges).
- **Expiry settlement**: the expiring future's settlement price, else the bhavcopy's underlying value, else the
  median K ± settlement price of expiring options worth > ₹1.
- **Overlays**: the held stock is priced by the same-expiry future (carry included). "Exit before results" is not
  applied: no historical board-meeting calendar is stored. The stock is the most-traded stock-option underlying of
  the in-sample window among those stored (a rule with hindsight on liquidity; stored stocks are the owner's held or
  watched ones, so the name is not written here).
- **Lot sizes** come from the bhavcopy (2024 on) or fno_contract's inferred lot (pre-2024, gcd of open interest);
  an unknown lot skips the trade.
- The calendar of sessions (not prices) is read ahead, only to exit on the last session before expiry day.

# Library v1 on the vault (studies `library_v1_same_print`, `library_v1_next_print`)

Part of the vault pack (PREREG.md, #364). Run once each, after the engine pin (PREREG.md §2.7), on the vault
database only.

## 1. What runs

The 8 registered specs of `backtest/specs.py` exactly as registered (registration hash `6d92407630d7…` must equal
`backtest/prereg.HASH`; the guard checks it), book, slippage tiers, exits, IV filter and sizing unchanged, replayed
continuously over the vault (1-Jan-2008 to 31-Dec-2021) with P1.3's dated lots and charges. The overlays (`stock_cc_m`,
`stock_pp_m`) have no stock options in the vault and are reported as not run.

- `library_v1_same_print`: fills at the decision day's close or settle, as registered and as published for 2024-26
  (comparable). This read alone can make a spec a vault pass.
- `library_v1_next_print`: P2.7(a)'s lag variant: decide on day-t data, fill traded legs at the t+1 open, else the t+1
  close, same slippage tiers. A registered sensitivity: reported per spec, never a pass by itself. Paired with the
  same-print read: D = PnL(same) - PnL(next) per matched trade, expiry-clustered t (one-sided: same-print overstates
  rupees per trade). A spec whose expectancy sign flips is labelled "execution-sensitive".

Each read also runs the registered stress replay (slippage x 2).

## 2. Pass on the vault (all must hold, same-print read)

The six criteria of `fno_backtest/PREREG.md`, with "out of sample" read as the vault window and the in-sample sign
check (criterion 5) read as the stored 2022-24 in-sample expectancy:

1. >= 30 vault trades.
2. Expectancy after costs > 0 with expiry-clustered t >= 2 AND the 95 % expiry-cluster bootstrap CI (5,000 draws,
   seed 307) above 0.
3. Max drawdown of the daily marked equity over the vault <= 20 % of capital.
4. Deflated Sharpe >= 0.95 with N = 8 and V = the variance of the vault daily Sharpe ratios of every evaluated
   variant, controls included.
5. 2022-24 in-sample expectancy > 0 (published values; sign agreement).
6. Vault expectancy still > 0 with slippage doubled.

Plus the **sub-period rule** (§3). A "candidate" needs a vault pass AND a pass on the published 2024-26 out-of-sample
window; none of the 8 passed that window, so this read cannot create a candidate. Its purpose is stress evidence and
the kill criteria (PREREG.md §4).

## 3. Sub-period rule

Per spec, expectancy per trade after costs > 0 in >= 60 % of its qualifying calendar years. A qualifying year is a
vault calendar year (2008-2021; for `nifty_ic_w` 2019-2021) in which the spec entered >= 3 trades (a trade belongs to
the year of its entry day). Years with fewer trades are listed, not counted. A spec with fewer than 3 qualifying years
(5 for a monthly spec) has an unknown sub-period result, which fails.

## 4. Reported for every spec, whatever the outcome

- Per era (2008-2015, 2016-2021) and per calendar year: trades, rupees per trade, t, CI, win %, max drawdown.
- **Stressed metrics** (#365): the worst trade; ES5 = mean of the worst ceil(0.05 n) trade P&Ls; max drawdown; the
  loss over the worst 20 consecutive sessions of the marked equity.
- **Drawdown for each named crisis**: peak-to-trough of the spec's marked equity within 1-Sep-2008 to 31-Mar-2009 and
  within 1-Feb-2020 to 30-Jun-2020, and the P&L of every trade open on 24-Oct-2008 and 23-Mar-2020.
- BANKNIFTY and FINNIFTY rows as a cross-market check (never gating).
- P2.6's corrected deflated Sharpe (V = 1/(T-1)) beside the registered one, not gating.

## 5. P3.4 drawdown-band test (derived from this read's stored output; no read of its own)

For each of the 4 seller specs: from its 2022-01 to 2026-10 trade P&Ls (all stored, seen), draw 10,000 paths of n
trades (n = the spec's vault trade count) by a block bootstrap over consecutive expiries with block length b = 3
(seed 364), and take the 5-95 % band of the path max drawdown in rupees. The comparator band: the current i.i.d.
lognormal model (`fincalc.options.drawdown_probability`: i.i.d. lognormal expiry P&Ls at each trade's entry sigma = RV20, drift = r) extended to n trades. Pass if
the realised vault max drawdown lands inside the bootstrap band for >= 3 of the 4 specs. This vault split is primary;
P3.4's 2022-24 to 2024-26 split is secondary (seen).

## 6. Known limits stated before the read

Regime breaks (lot sizes, monthly-only expiries before 2016-19, SEBI's 2024 rules, STT changes) make this stress and
holdout evidence, not a like-for-like sample. Bhavcopy has no bid/ask, so slippage stays the registered tier
assumption. The 2008 and 2020 outcomes are public: the read tests survival and calibration, not discovery. Expected
outcome: confirms "no edge".

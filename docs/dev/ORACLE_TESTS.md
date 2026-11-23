# Oracle tests (#503)

All of FinResearch's finance maths is written in-house and tested against hand-computed values. The oracle suite
is a second line of defence: it compares our functions with mature outside libraries over grids of inputs, to find
the bugs and the unstated conventions that a handful of hand-worked cases cannot.

```bash
uv run --group oracle pytest oracle_tests -q                               # the whole suite (about a minute)
uv run --group oracle pytest oracle_tests -q --oracle-report=/tmp/t.md     # also write the comparison table
uv sync --locked                                                           # back to the default environment
```

## Rules

- The libraries are **dev-only**. They sit in the uv dependency group `oracle` (`pyproject.toml`), which is not a
  default group: `uv sync --locked`, `uv run pytest` and CI's `test` job never install them. Nothing under `src/`
  (or `tests/`, `scripts/`, `migrations/`, `evals/`) may import one; `tests/test_oracle_isolation.py` in the normal
  suite fails if anything does, and checks the import list against `uv.lock`.
- The oracle tests live in `oracle_tests/`, outside pytest's `testpaths`, so the normal suite's pass and skip counts
  do not depend on them. CI runs them in the job `oracle`, which no merge waits on.
- `uv run --group oracle ...` leaves the libraries in `.venv` (uv does not remove packages on `uv run`); the normal
  suite is unaffected because nothing imports them. `uv sync --locked` removes them again.
- Inputs are synthetic or hand-made. The suite reads no market data, no `data/` and no database (its `conftest.py`
  points `FINRESEARCH_DATABASE_URL` at a host that cannot exist).
- Every comparison states its tolerance and why. A difference beyond tolerance is classified, never tuned away:
  **(a)** an in-house bug: fixed in `src/` with a regression test in `tests/` whose expected value is derived
  independently, plus a `FUNCTIONAL_TESTING.md` entry; **(b)** a convention difference: written down in the oracle
  test; **(c)** an oracle limitation: written down, with a second oracle as referee where one exists.
- Not fixed in place, only reported for the owner's decision: anything in the vault's engine pin
  (`src/finresearch/backtest/*.py`, `fincalc/options.py`, `fincalc/charges.py`), a registered spec (`*/specs.py`,
  `PREREG.md`) or a golden tax expectation.
- An oracle is a witness, not an authority: where the two sides differ the test says which one is right and how
  that was decided (a third implementation, arbitrary-precision arithmetic, or the source paper).

## Libraries and licences

Read on 23-Nov-2026 from PyPI's metadata and from the LICENSE file in each project's repository. Only permissive
licences (MIT, BSD, Apache-2.0, NCSA) are allowed. Wheels for CPython 3.12 exist for macOS arm64 and Linux x86_64
for every row (checked in `uv.lock`), so nothing is compiled.

| Library | Version | Licence | Why it is here |
| --- | --- | --- | --- |
| QuantLib | 1.44 | QuantLib licence: modified BSD 3-clause (`LICENSE.TXT`; PyPI says BSD-3-Clause) | bonds, XIRR, Black-76, SVI smile section, one-touch barrier |
| arch | 9.0.0 | University of Illinois/NCSA (`LICENSE.md`; PyPI says NCSA) | GARCH and GJR fits, SPA, Reality Check, StepM, MCS, stationary bootstrap, VaR backtests |
| scipy | 1.18.1 | BSD-3-Clause (`LICENSE.txt`) | distributions, root finding, special functions |
| statsmodels | 0.15.0 | BSD-3-Clause (`LICENSE.txt`) | OLS with HAC and clustered errors |
| mpmath | 1.4.1 | BSD-3-Clause (`LICENSE`) | arbitrary-precision referee where two double-precision answers differ |

Installed with them (dependencies of arch and statsmodels, never imported by the suite directly):

| Library | Version | Licence |
| --- | --- | --- |
| pandas | 3.1.1 | BSD-3-Clause |
| patsy | 1.0.3 | BSD-2-Clause; one file under the PSF licence 2 (`LICENSE.txt`) |
| formulaic | 1.2.2 | MIT |
| interface-meta | 2.0.1 | MIT |
| narwhals | 2.29.0 | MIT |
| wrapt | 2.5.2 | BSD-2-Clause |

Never to be added (licence): FinancePy and optionlab (GPL-3.0), rateslib (source-available, not open source),
mlfinlab (proprietary). mpmath is not in issue #503's candidate list; it was added because QuantLib's own normal
distribution is only accurate to about 1e-7 in the far tail, so a comparison of deep-wing option prices needs a
referee that is exact.

To add a library: read its licence in both places, confirm the wheels, pin it exactly in the `oracle` group, run
`uv lock`, add its import name (and those of every package it pulls in) to `IMPORT_NAMES` in
`tests/test_oracle_isolation.py`, and add a row here.

## What is compared (first slice, 23-Nov-2026)

38 tests, 74 comparisons, about 75 seconds. The run prints one row per comparison (function, oracle, grid size,
largest difference, tolerance and its reason, verdict); `--oracle-report` writes the same table to a file. In short:

| In-house | Oracle | Inputs | Result |
| --- | --- | --- | --- |
| `fincalc/bonds`: schedule, dirty price, YTM, durations, convexity | QuantLib `Schedule`, `CashFlows.npv`, `BondFunctions` (`ActualActual(ISMA)`, `Compounded`) | 12,864 bonds: 12 maturities (month ends, 29 February), 17 settlements (between coupons, leap days), 4 coupons, 4 frequencies, 4 yields | agree to rounding after the fix below |
| `bonds.accrued_interest` | QuantLib `ActualActual(ISMA)` on the one-year anniversary period (SEBI's rule in QuantLib's terms) | 2,412 | agree to 1e-15 |
| `funds.xirr`, `analytics_math.investor_xirr` | QuantLib NPV (`Actual365Fixed`, annual compounding) with scipy `brentq`; QuantLib's own solver where it converges | 3,300 synthetic investor histories across leap years | agree to the 8 decimals returned |
| `black76.price`, `vega`, `forward_delta`, `spot_delta`; `smile.vanna`, `volga` | mpmath (40 digits) as referee; QuantLib `blackFormula`, `BlackCalculator` | 9,216 points: strikes 30 % to 300 % of the forward, one minute to three years, 1 % to 300 % volatility | within first-order float rounding of the exact value |
| `black76.implied_vol` | mpmath prices with a known volatility; QuantLib `blackFormulaImpliedStdDev` | 6,090 solvable quotes of that grid | within the solver's stated tolerance; 1.9e-9 in volatility out of the money |
| `svi.w`, `smile.sigma`, `density`, `cdf` | QuantLib `SviSmileSection`; mpmath strike derivatives of the price | 4 slices (one arbitrageable), 49 strikes each | exact form agrees to 1e-16; QuantLib's finite differences to their truncation error |
| `risk_alerts.touch_prob` | QuantLib `AnalyticDigitalAmericanEngine` (one-touch, zero rates) | 686 | the shortcut's error is exactly (option price) / K; see "needs decision" |
| `taildist._gjr_nll`, `gjr_filter`, `ewma_variance`, `backcast`; `gjr_fit`; `realvol.garch11_fit` | arch `ZeroMean` + `GARCH`, `EWMAVariance` | 5 synthetic paths, 5 parameter points, 10 fits | likelihood and filter to 1e-15; fits within 2e-10 in log-likelihood |
| `overfit.reality_check`, `spa`, `romano_wolf`, `kernel_var`; the three stationary-bootstrap index functions | arch `SPA`, `StepM`, `StationaryBootstrap`, on arch's own resampled indices | 4 designs of 6 trials over 250 sessions; 3.7 million index steps | Reality Check identical; SPA differs by two conventions (below) |
| `analytics_math.kupiec`, `christoffersen_ind`, `chi2_sf` | arch `kupiec_pof`, `christoffersen_independence`; scipy `chi2.sf` | 468 counts, 116 hit sequences, the tail out to x = 1,400 | agree to rounding, edge cases included |
| `factor_math.ols_hac`, `metrics.clustered_t`, `signals.wilson`, `funds.wilson_interval`, `taildist.gpd_fit` | statsmodels OLS (HAC, cluster); scipy `binomtest`, `genpareto.fit` | 48 regressions, 20 clusterings, 34 intervals, 18 samples | agree |

### (a) In-house bugs found and fixed

1. **Previous coupon date of a month-end maturity** (`fincalc/bonds.py`). 74 of 804 schedules disagreed with
   QuantLib: the coupon before a clamped date (28-Feb for a 31-Aug bond) was taken as 28-Aug, not 31-Aug. Accrued
   interest was 1 to 3 days too high and the price, YTM and durations moved with it (up to 6 basis points of yield
   on the grid; 1.3 bp for a 9 % half-yearly NCD at par). Regression test:
   `tests/test_bonds.py::test_month_end_maturity_keeps_its_coupon_day_for_the_previous_coupon`.
2. **A return of exactly zero printed as "-0.00%"** (`funds.xirr`, `bonds.ytm`, `bonds.after_tax_ytm`). Regression
   tests: `tests/test_funds.py::test_a_flat_investment_has_an_xirr_of_zero_not_minus_zero`,
   `tests/test_bonds.py::test_a_zero_yield_is_zero_not_minus_zero`.

Both are in `FUNCTIONAL_TESTING.md` (23-Nov-2026).

### Needs decision (measured, not changed)

1. **Coupons are equal, accrual is by the day** (`bonds.cash_flows` against `bonds.accrued_interest`). Every period
   pays face x coupon / freq, while interest accrues actual days over the 365- or 366-day year. For half-yearly,
   quarterly and monthly bonds a full period therefore does not accrue one coupon (a 184-day half-year accrues
   4.537 against 4.50 paid per 100 at 9 %; February accrues 0.690 against 0.75), and a par bond's clean price steps
   at each coupon date (99.963 to 100.036 half-yearly, 99.986 to 100.057 monthly). SEBI's circular sets its
   denominator "for payment of interest", which means day-count coupons for listed NCDs; an equal-coupon bond
   would instead want ISMA accrual. Annual-coupon bonds are consistent either way. Changing it moves every
   non-annual YTM slightly and needs to know which instruments pay which way.
2. **The touch-probability shortcut is one-sided only below the forward** (`risk_alerts.touch_prob`, registered
   with #398). 2 x N(-d2) overstates the exact first-passage probability by (put price) / K for a strike below the
   forward and UNDERSTATES it by (call price) / K above it: up to 0.097 and 0.116 in probability on the grid (60 %
   volatility, 90 days). At the alert's 0.60 level, 30 days and 15 % volatility, the exact probability of a short
   call is already 0.608 when the shortcut reaches 0.60 (the strike is 2.19 % away; exact 0.60 is at 2.24 %). The
   exact form, N(-d2) + (F/K) N(-d1), costs one more normal CDF.
3. **SPA p-values against arch 9** (`stratlab/overfit.spa`). The module follows Hansen (2005) as it cites: every
   bootstrap statistic is divided by the sample's standard error, and the statistic is floored at zero. arch 9.0.0
   re-studentises inside each bootstrap draw and does not floor. On arch's own resamples the consistent p-value of
   an all-noise design is 0.1025 in-house against 0.1325 in arch, and 0.4875 against 0.8075 when every trial
   loses; the 5 % reading agreed on all four designs. Both are defensible; arch's variant is the more conservative
   on noise. A size study on simulated nulls would say which the Strategy Lab's gate should read.

### (b) Convention differences (written down in the tests)

- Accrued interest: SEBI's Actual/Actual divides by the anniversary year, QuantLib's `ActualActual(ISMA)` bond
  accrual by the coupon period: up to 0.091 per 100 face on the grid, every case explained by one identity.
- XIRR: a root outside (-99.99 %, 1000 %) is unknown (21 of 3,000 histories); two roots inside the bracket are
  unknown too, though the error text says "no solution".
- Implied volatility: an in-the-money quote whose time value is below one ulp of its price is unknown (622 grid
  points), never a number.
- SPA and the stepdown: see "needs decision" 3; the Reality Check is identical to arch's.

### (c) Oracle limitations found

- QuantLib's cumulative normal is an asymptotic series once N(z) <= 1e-8: 1.2e-7 relative error near z = -5.62, so
  its far-wing Black prices are off by up to 5.7e-6 relative and its implied volatilities by 1.7e-7; it returns 0
  for 448 far-wing prices it cannot invert. The in-house values are within rounding of the 40-digit ones.
- QuantLib 1.44's `BlackCalculator.volga(T)` returns the true volga divided by sqrt(T) (right only at T = 1).
- QuantLib's `yieldRate` cannot bracket a negative or zero yield (665 of 2,979 histories).
- QuantLib's smile-section density is a finite difference: 1e-4 of the peak at its default gap.
- A double cannot carry the time value of a deep in-the-money quote: the implied volatility is then only as good
  as the quote (the bound holds; the largest error was 2.3 vol points on a 300 % volatility one day from expiry).

### Not done yet (#503 stays open)

- arch: `volrace/stats.mcs` (frozen from arch 8.0 in the normal suite; no live comparison yet).
- scipy / statsmodels: `inference.twoway_clustered_t`, `lo_sharpe`, `bca_interval`, `ipo_model.fit_quantile` and
  `fit_logistic`, `realvol.ols` and `mincer_zarnowitz`, `stock_backtest.newey_west_t`.
- skfolio (+ scikit-learn, scipy linkage): Ledoit-Wolf, minimum variance, risk parity, HRP, deflated Sharpe.
- vollib / lets-be-rational, empyrical-reloaded, pandas_market_calendars, talipp: none added yet.
- `fincalc/options.py` (`implied_vol`, `prob_above`, greeks) is in the vault's engine pin: comparing it is fine,
  changing it is not; not compared yet.

# Edge cases to sweep for every feature

Decide each as test / handle / out of scope, with a reason. Skip only the ones that cannot apply, and say so.

## Time and calendar
- Market holidays (unknown holiday list for the year), weekends, special sessions (Muhurat), half days
- T+1 settlement (equity), T+0 optional segment; fund cut-off times and NAV dates; bond record dates
- IST everywhere (`today_ist()`/`to_ist()`); financial-year boundaries (named by end year); 31 March and 1 April
- Effective dates of rules: LTCG 12.5% and ₹1.25 lakh exemption from 23-07-2024; grandfathering on 31-01-2018
  prices (Sec 112A); indexation removal; changes under the Income-tax Act 2025
- Data published late or revised after the fact (results restatements, NAV corrections)

## Instruments and corporate actions
- Splits, bonuses, rights, demergers, mergers, symbol changes, ISIN changes, delistings, suspensions
- Dividends (ex-date vs record date vs payment date), IDCW vs growth plans, fund mergers and scheme renames
- F&O: lot-size changes, expiry-day moves, contract adjustments after corporate actions
- Bonds: clean vs dirty price, accrued interest, call/put dates, unrated or downgraded issues

## Data quality
- Missing, stale or partial price/NAV/feed: shown as unknown with a reason, never 0 or ok
- Source returns HTTP 200 with an error page or empty body; rate limits; schema/format changes (e.g. UDiFF bhavcopy)
- Duplicates across sources (broker sync vs statement import), out-of-order or same-timestamp records
- Units and scale: ₹ vs lakh vs crore vs million, percent vs fraction, paise rounding

## Numbers
- Decimal end to end for money; rounding mode and where it happens; no float sums
- Annualisation basis (252 trading days vs 365 calendar days), compounding vs simple, XIRR non-convergence
- Look-ahead bias and survivorship bias in any backtest or signal; out-of-sample split fixed before evaluation
- Empty portfolio, single holding, zero or negative quantities, short positions, very large values

## Tax and lots
- FIFO lots across accounts and brokers, partial sells, transfers between demat accounts, gifts and inheritance cost
- Short-term vs long-term thresholds per asset class; set-off order (STCL against STCG or LTCG; LTCL only against
  LTCG); 8-year carry-forward that needs an on-time return
- Loss harvesting: no wash-sale rule in India, but GAAR risk for sell-and-rebuy; exit loads and STT in the cost

## App and privacy
- Personal data never reaches a model (portfolio, statements, journal, wealth, broker connections)
- GETs never write; a failed fetch is not cached as data; long lists paginate with a continuation hint
- Two monitor processes or a resumed run do not repeat work; idempotent writes
- UI: loading, empty, partial, error and stale states; mobile width; e2e fixtures for every new `/api/` call

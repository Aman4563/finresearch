# Book-level risk budget counterfactual on the vault (study `risk_budget_counterfactual`; P4.2 Stage 2)

Part of the vault pack (PREREG.md, #364). One logged read with the pinned engine.

## Design

Replay the four registered seller specs (`nifty_pcs_m`, `nifty_ccs_m`, `banknifty_ic_m`, `nifty_ic_w`) concurrently
on one Rs 10 lakh book, monthlies over 2008-2021 and `nifty_ic_w` from Feb-2019 [verify], same-print fills:

- **Budgeted book:** a new trade is accepted only if open risk + |max loss of the new trade| <= B_book = 8 % of
  capital, the book's common-shock loss at m = -5 % (spot shock with the registered vol response, t + 1 day) <=
  B_shock = 6 %, margin utilisation <= u_max = 0.5, and month-to-date realised plus marked P&L after costs > -6 %
  (L_month); lots from P4.2's single sizing formula.
- **Size-matched control:** the same book without those limits, scaled to the budgeted book's mean open risk
  (fractional lots allowed in the counterfactual only).
- Margins: `fincalc.margin_approx` with its labelled assumptions; P1.8 SPAN scan ranges only where a stored file
  covers the day (none expected before the backfilled monthly samples).

## Tests

- **Primary (both must hold):** the budgeted book's worst calendar month AND its max drawdown are lower than the
  control's, one-sided block bootstrap over calendar months (block 3 months, 5,000 draws, seed 395), p < 0.05 each.
- **Secondary:** trades taken fall by <= 40 % against the unlimited book.
- Reported, not gated: the raw cut against the unscaled book; expectancy with its cluster-bootstrap CI (expected
  about 0 either way); results by era.
- Secondary period: 2022-01 to 2026-10, labelled seen.

## Limits

Margins before P1.8's files are approximations with assumed scan ranges; 2008-2015 has no weeklies, so the
concurrent book is three monthly specs there. The limits create no edge; they change sizing and survival only.

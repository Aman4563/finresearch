# Pre-registration: the joint risk view's explanatory test (P4.4 #397), version `joint-risk-v1`

Registered 22-Oct-2026, before any real P&L was compared: no portfolio, `fno_mark` or `paper_strategy` row of the
live database was read for it, and its tests use synthetic numbers only. The gate's constants are
`BIG_MOVE`, `MIN_DAYS` and `R2_GATE` in `src/finresearch/fincalc/joint_risk.py`; where this file and the code differ,
the code wins. This file is never edited: changes go to `ADDENDUM.md` as post hoc, before the changed test reads real
data.

## The claim

The joint view's model of a NIFTY move (each holding's own path plus a full revaluation of every F&O leg, the
book's spot-vol response and β band as in `fincalc.book_scenarios`) explains the joint P&L of both books on large
market days. It creates no edge claim: it decides only whether the joint stress figures can be trusted as a
description of how the two books move together.

## Test

- **Qualifying day:** a session whose NIFTY close-to-close move (NSE's index close, local `index_eod`) satisfies
  |move| > 1.5 %.
- **Actual joint P&L:** the day's change in the holdings' value (own closes or NAVs, the canonical value history)
  plus the F&O book's change at NSE settlement prices (`fno_mark`, the P2.8 daily mark table), flows excluded.
- **Explained joint P&L:** each holding's own move that day plus the book's full revaluation from the previous
  session's marks to the day's spot and IVs.
- **Statistic:** R² = 1 - Σ(actual - explained)² / Σ(actual - mean actual)² over the qualifying days
  (`joint_risk.variance_explained`). The holdings' single-beta R² on the same days is reported beside it.
- **Gate:** R² ≥ 0.90. Evaluated once, when 20 qualifying days exist (`joint_risk.forward_test`); before that the
  result is "not evaluable yet" with the count, never a pass.

## (a) The historical test

#397 (a) adds a synthetic F&O sleeve (the four registered seller specs' backtest positions marked at settles,
2022-26) to the portfolio TWR series and applies the same gate. That sleeve is P4.2 Stage 2's series, which does not
exist yet: (a) is not run, and is not approximated, until it does.

## (b) The forward test on the real books

The paper book started on 12-Oct-2026 and Groww keeps only current positions, so the count starts on 12-Oct-2026.
The joint view shows the number of qualifying days so far (sessions since then with marks and |NIFTY move| > 1.5 %)
out of the 20 needed.

# Addendum to `joint-risk-v1` (post hoc to PREREG.md, which is not edited)

## 22-Oct-2026: how the forward test's inputs are stored and read (#397 (b))

Written before the stored joint P&L read any real row: the table (`joint_pnl_day`) is new, and its tests use
synthetic numbers only. Nothing below changes the gate (`BIG_MOVE`, `MIN_DAYS`, `R2_GATE`), the qualifying-day rule
or the statistic. It fixes how three phrases of PREREG.md's *Test* section are operationalised.

1. **"the book's full revaluation from the previous session's marks to the day's spot and IVs".** Repricing each leg
   at the day's own underlying close and its own implied IV gives back the settle change up to rounding, because
   `fno_mark`'s IV is implied from that settle and that close (`fnobook.marks`); a test of it could not fail
   (`tests/test_joint_pnl.py::test_the_market_revaluation_reproduces_the_settles_by_construction`). The explained
   book P&L is therefore the model the *Claim* section names: the previous evening's marked legs (with their settles,
   stored forwards and registered betas as of that evening) fully revalued one session on at NIFTY's realised
   close-to-close move, each underlying at β × move at the worse end of its band, with the spot-vol response
   (`book_scenarios.revalue`, i.e. L(m)'s F&O term evaluated at the realised m, not interpolated from the stored
   grid). The market repricing is stored beside it (`fno_market`) as a check of the marks, never as the explained
   figure.
2. **"each holding's own move that day".** The previous evening's stored holdings (value and key) × each holding's
   own close-to-close return (closes or NAVs, split/bonus factors applied). A holding bought during the session is
   not in that snapshot (its P&L is in the actual only); one sold is valued at the close, not the sale price.
3. **"flows excluded".** Holdings: the canonical value history's change less the change in cumulative net money in.
   F&O: only legs carried over both evenings at the same quantity; a session in which a leg was opened or closed (its
   fill is not in the marks) is unusable, as is any other unknown part.

Evaluation rules fixed here:
- The holdings' single-beta figure beside the test is the previous evening's stored D_holdings (Σ β_i V_i) × NIFTY's
  move, and its R² is over the same days.
- A session whose row is unusable (an unknown part, a session in between two stored evenings, the Groww book unread
  that evening) is not a test day; every one is counted with its reason in the view.
- The gate is evaluated once, on the first 20 usable qualifying sessions since 12-Oct-2026 in date order; later
  sessions do not re-test it.

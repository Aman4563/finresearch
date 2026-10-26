# Pre-registration: the book-level risk budget on the seen 2022-2026 history (P4.2 Stage 2, #395)

Registered 26-Oct-2026, before any code that replays a stored session for this study existed: no `fno_bhav`,
`fno_contract`, `fno_iv_eod` or `span_param` row has been read for it, in the live database or any copy, and the
2008-2021 vault has not been read. The registration is `src/finresearch/riskbudget/specs.py` (`risk-budget-seen-v1`);
its sha256 `801068ede3f169e03409550200847c6137125cbd3f8071aed1de9a7741459153` is pinned in `riskbudget/prereg.py`
together with this file's own sha256, and the study refuses to run while either differs. Every numeric setting is a
constant in `specs.py` or read at call time from the module that owns it (`fincalc/tradeplan.py`'s limits, the four
specs, `Book` and `Criteria` of `backtest/specs.py`, `fincalc/book_scenarios.py`'s spot-vol response, the beta
registration `book-beta-v1`, `fincalc/margin_approx.py`, `backtest/dbhistory.py`'s SPAN age, the IV percentile window
and the futures quarantine), so editing one moves the hash. Where this file and `specs.py` differ, `specs.py` wins.
This file is never edited: changes go to `ADDENDUM.md` as post hoc, with a new `SPEC_VERSION` and hash committed before
the changed study replays a stored session.

It implements the seen period ("Secondary period: 2022-01 to 2026-10, labelled seen") of the vault study
`risk_budget_counterfactual` (`evals/experiments/vault/risk_budget.md`, sha256
`eae827dc672c4aee6c032b5c6b893a9fb5d6b657f5cb2dd4bcbdf7a0c111bd9d`, sealed in the vault pack #364) without editing it,
and fixes what that file leaves open. It is **not** the vault read: it never opens the 2008-2021 vault (`DbHistory`
refuses a database holding vault-period days without a grant). It searches no strategy, adds nothing to
`VARIANTS_TRIED`, and changes no card, no live sizing and no evidence tag. Every output is labelled "seen, secondary".

## Question

What would the one sizing rule's book-level limits have changed for the four registered seller specs traded
concurrently on one ₹10 lakh book over the stored 2022-2026 history: fewer or smaller positions, and a less severe
worst calendar month and maximum drawdown than an uncapped book of the same mean open risk?

## Data and period

- The stored NSE F&O bhavcopy of NIFTY and BANKNIFTY in the database the owner names (`--history-url`, default the
  app's), read only through `backtest.dbhistory.DbHistory` in READ ONLY transactions, with its 30-day IV series,
  lots and the dated `fincalc.charges`, exactly as the registered backtest reads them.
- Margins: `backtest.engine.margin` (`fincalc.margin_approx`) with the newest stored NSE SPAN scan ranges on or before
  the day within 45 days (`DbHistory(span_ranges=True)`, #370: the issue's "nearest monthly backfilled file", never a
  later one); a day no stored file covers uses margin_approx's labelled assumptions, and the report counts entries
  priced each way. A history database without the `span_param` table is refused before any session is replayed.
- Entries from 1-Jan-2022 to the period's last session, the last stored session on or before 31-Oct-2026. On that
  session no entry is made and every position still open after management is squared off at its reference prices with
  the engine's slippage and charges (reason `period_end`: the sealed file's "closed at that day's marks"). A run whose
  stored history ends before the last weekday on or before 31-Oct-2026 is labelled partial, not the registered run.

## The replay: one book, one pass over the sessions

- Sessions: every stored session of NIFTY or BANKNIFTY from the first stored day, oldest first, through
  `backtest.history.Clocked` (nothing after a session's close is read). An underlying with no stored day that session
  is not managed or re-marked (as the engine), and its forwards are unknown that session.
- Specs: `nifty_pcs_m`, `nifty_ccs_m`, `banknifty_ic_m`, `nifty_ic_w` exactly as registered in `backtest/specs.py`
  (`backtest/prereg.py`): entry rules, strikes by delta, wings, DTE windows, max concurrent, per expiry and days
  between entries per spec, the IV percentile filter, the exits, same-print fills (the registered fill), the slippage
  tiers, dated charges and expiry settlement (P0.2).
- At each close every open position of the book is managed by `backtest.engine.manage`; then entries are considered
  spec by spec in the order above, and an accepted position is in the book when the next spec is considered.
- Trade size, every arm: n_trade = min(20, floor(5 % of ₹10 lakh ÷ (max loss + entry costs of one lot))), as
  `backtest.engine.try_enter`; then the engine's margin rule applied to the **whole book** (the open positions of all
  four specs, where the published per-spec runs used each spec's own): lots fall one at a time while the open entry
  margins plus this trade's margin exceed ₹10 lakh. An open position whose margin is unknown refuses every entry.
- Equity E = ₹10 lakh + realised net P&L + the open positions' (marked gross − entry costs), recorded with the open
  risk OR (the open positions' defined max loss at their entry fills, `Position.max_loss`; all four specs are
  defined-risk, so this equals `fincalc.tradeplan.position_risk`) at every session of the period after its entries.

## Arms

1. **Uncapped** (U): the trade size above and nothing else.
2. **Budgeted** (B, primary): U's candidate entries, each sized by P4.2's single formula and refused below one lot:
   - Month stop (L_month 6 %): no entry while E(now) − E(the previous calendar month's last session) ≤ −6 % of
     ₹10 lakh (₹10 lakh before the first month). E(now) is the book after this close's management and earlier
     entries. (A paper card counts an open trade's whole marked P&L because its month-start mark is not stored; the
     replay has the mark.)
   - Lots = min(the real lots of `fincalc.tradeplan.size(max loss of one lot, its entry costs, 5 % of capital,
     proven=True, cap=20, OpenBook(capital, OR, funds=Funds(cash=capital, margin_used=the open entry margins,
     collateral=0, im_one=one lot's margin)))`, `tradeplan.shock_lots(L_book, L_1, 6 % of capital, moves=(−5 %,))`).
     `size()` gives min(n_trade, floor((8 % of capital − OR) ÷ (|ML_1| + c_1))) and the margin term
     floor((u_max × min(cash + collateral, cash ÷ c_min) − used) ÷ IM_1) = floor((0.5 × ₹10 lakh − used) ÷ IM_1)
     (u_max 0.5, c_min 0.5, the backtest book is all cash, margins at entry as the engine keeps them); real lots
     because the counterfactual trades the book.
   - Common shock (B_shock 6 % at m = −5 %): L(m) is `fincalc.book_scenarios.shock_pnls` from this close (15:30 IST)
     to the next stored session's close: BANKNIFTY moves β × m at the worse end of its registered beta band, IVs
     follow the registered spot-vol response, no extra vol shift. Legs at their marks (the day's reference prices),
     IVs inverted from them on the engine's forward (`backtest.pricing.forward` at the Book's 6.5 %), every expiry
     discounted and every forward rolled at the Book's 6.5 % (`Market.fixed_carry`, this study's rule: #480's stored
     carry needs the underlying's close, which the legacy 2022-24 bhavcopy does not carry). L_book is the open book's
     P&L and L_1 one lot of the candidate's legs; n lots are accepted only if L_book + n L_1 ≥ −6 % of capital, and a
     book already at or beyond −6 % takes nothing.
   - Beta: BANKNIFTY's estimate as of the session (`fincalc.beta.estimate`, `book-beta-v1`) from the NIFTY and
     BANKNIFTY front-future settles the replay has read up to that close, point in time, without lot adjustment
     (index lot revisions do not move the price, so the roll rule's lot factor never applies). It is unknown for
     about the first six months of 2022 (120 paired returns needed) and after gaps or a missing return in the last 5
     sessions; then every shock involving BANKNIFTY is unknown.
   - Unknown is never room: an unknown OR, margin, forward, mark-implied IV or beta sizes 0 (refused, counted).
3. **Card rule** (K, reported, never gated): B with the shock at every move of `tradeplan.SHOCK_MOVES` (−3, −5, −10,
   +5 %), as live cards apply it (#443 decision 2), and the Kelly ceiling as live cards apply it (#443 decision 6): 0
   lots while μ_lo ≤ 0 or unknown, μ_lo = the lower end of the 95 % expiry-clustered bootstrap CI
   (`backtest.metrics.cluster_bootstrap`, the backtest's 5,000 draws and seed 307) of the net ₹ per trade of that
   spec's U trades closed at or before the session; unknown below 30 such trades. The full fraction max(0, μ_lo)/σ²
   is not applied (live cards apply only its zero).
4. **Size-matched control** (C, computed): U's daily P&L × k, k = mean OR_B ÷ mean OR_U over every session of the
   period (fractional lots; brokerage per order and the slippage floor make costs only approximately proportional).
   mean OR_U = 0: k unknown.

## Tests

- Monthly P&L = E at a calendar month's last session − E at the previous month's last session (₹10 lakh before the
  first); each month's daily P&L path is kept. W = the worst (minimum) monthly P&L; D = the largest fall of E from its
  running peak (₹; the peak starts at ₹10 lakh), on the daily marked equity.
- **Primary (both must hold):** W_B > W_C and D_B < D_C, each by a one-sided moving-block bootstrap over the M
  calendar months of the period: blocks of 3 consecutive months with starts uniform over the M − 2 possible starts,
  ⌈M/3⌉ blocks concatenated and cut to M months, the same draw for B and C (paired); in each draw W and D are
  recomputed from the drawn months' daily paths in their drawn order. ΔW = W_B − W_C, ΔD = D_C − D_B; p = the share of
  5,000 draws (seed 395) with the difference ≤ 0; each passes when p < 0.05. Fewer than 12 months, or k unknown:
  unknown, which fails.
- **Secondary:** trades taken fall by at most 40 %: 1 − entries_B ÷ entries_U ≤ 0.40 (entries = positions opened; no
  uncapped entry: unknown, fails).
- **Reported, not gated:** the raw cut (W and D of B against U, unscaled); per arm the entries, closed trades,
  expectancy per trade with its 95 % expiry-clustered bootstrap CI as the backtest reports it
  (`backtest.metrics.trade_stats`, 5,000 draws, seed 307; expected about 0 either way), total net P&L, mean OR,
  refusals by reason and lots cut by each binding term; K against U the same way. One era (2022-2026).

Power, stated up front: about 58 calendar months; the published per-spec runs took about 250 trades out of sample
(Jul-2024 to Oct-2026), so U should take roughly 500 over the period.

## Reading rule

Seen data, secondary period: the outcome is reported whatever it is, labelled seen. Nothing is adopted, no limit or
card changes because of it, and it creates no candidate. A primary pass says the limits cut tail loss beyond what a
smaller uncapped book would; a primary fail says they only shrank the book; a secondary fail says they took too many
trades away. The registered test of the rule stays the vault study `risk_budget_counterfactual` (one logged read with
the pinned engine after `library_v1_same_print`), which this study never performs.

## Run

`uv run finresearch fno risk-budget --history-url <read-only F&O history database> --out <report.json>` replays once
and writes the JSON report (nothing is stored in any database). Results and any post hoc note go to `RESULTS.md` or
`ADDENDUM.md` in this folder, never to this file.

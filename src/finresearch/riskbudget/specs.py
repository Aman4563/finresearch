"""The registration of the seen-period risk-budget study (P4.2 Stage 2, #395): the four registered seller specs replayed
concurrently on one book over the stored 2022-2026 history, uncapped and under the one sizing rule's book-level limits,
hashed by `registration_hash()` and pinned in `prereg.py` (with the sha256 of
evals/experiments/risk_budget_seen/PREREG.md, which restates it in prose; this file wins) before any code that replays
a stored session existed.

It implements the seen ("secondary") period of the sealed vault study `risk_budget_counterfactual`
(evals/experiments/vault/risk_budget.md, pinned below by its sha256) without editing it, and fixes what that file
leaves open. It is not the vault read, searches no strategy, adds nothing to backtest/specs.VARIANTS_TRIED, and
changes no card, no live sizing and no evidence tag. Every numeric setting the study reads is either a constant here or
read at call time from the module that owns it (`settings()`), so editing one moves the hash. Never edit a registered
value in place: a change is a new SPEC_VERSION recorded as post hoc in the experiment's ADDENDUM.md (never PREREG.md),
with its hash committed before the changed study replays a stored session.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date

SPEC_VERSION = "risk-budget-seen-v1"
STUDY_ID = "risk_budget_seen"
VAULT_STUDY = "risk_budget_counterfactual"
VAULT_FILE = "evals/experiments/vault/risk_budget.md"
VAULT_FILE_SHA256 = (
    "eae827dc672c4aee6c032b5c6b893a9fb5d6b657f5cb2dd4bcbdf7a0c111bd9d"  # risk_budget.md at registration
)
LABEL = "seen, secondary (2022-2026 was read many times before); not the vault read; adopts nothing"

# the four registered sellers of backtest/specs.py, in the order their entries are considered at one close
SPEC_KEYS = ("nifty_pcs_m", "nifty_ccs_m", "banknifty_ic_m", "nifty_ic_w")
PERIOD_START, PERIOD_END = date(2022, 1, 1), date(2026, 10, 31)  # the sealed file's "2022-01 to 2026-10"
FILL = "same_print"  # the registered fill (backtest.engine.FILLS[0])
SPAN_RANGES = True  # margins on the stored NSE SPAN ranges where a file covers the day (DbHistory, #370)
ARMS = ("uncapped", "budgeted", "card_rule")  # replayed; the size-matched control is computed from "uncapped"
PRIMARY_MOVES = (
    -0.05,
)  # the sealed file: B_shock at a NIFTY move of -5 % (the card rule uses tradeplan.SHOCK_MOVES)
KELLY_MIN_TRADES = (
    30  # card rule: mu_lo unknown below this many closed uncapped trades (Criteria.min_oos_trades)
)
BLOCK_MONTHS = 3  # the sealed file's block bootstrap over calendar months
DRAWS = 5000
SEED = 395
ALPHA = 0.05  # each primary p must be below this
MAX_TRADES_FALL = 0.40  # secondary: 1 - entries(budgeted) / entries(uncapped) at most this
MIN_MONTHS = 12  # fewer calendar months in the period: the primary is unknown (fails)

QUESTION = ("What would the one sizing rule's book-level limits (B_book, the common shock at -5 %, the margin "
            "cap and the month stop) have changed for the four registered seller specs traded concurrently on one "
            "Rs 10 lakh book over the stored 2022-2026 history: fewer or smaller positions, and a less severe worst "
            "calendar month and maximum drawdown than an uncapped book of the same mean open risk?")  # fmt: skip
REPLAY = {
    "data": "the stored NSE F&O bhavcopy of NIFTY and BANKNIFTY in the database the owner names, read only through "
            "backtest.dbhistory.DbHistory (READ ONLY transactions; a database holding vault-period days is refused "
            "without a vault grant), with its 30-day IV series, lots and the dated fincalc.charges, as the "
            "registered backtest reads them",
    "margins": "backtest.engine.margin (fincalc.margin_approx) with the newest stored NSE SPAN ranges on or before "
               "the day within SPAN_MAX_AGE_DAYS (DbHistory span_ranges=True), else margin_approx's labelled "
               "assumptions, counted; a history database without the span_param table is refused before any "
               "session is replayed",
    "sessions": "every stored session of NIFTY or BANKNIFTY from the first stored day, oldest first, through "
                "backtest.history.Clocked (nothing after a session's close is read); an underlying without a stored "
                "day that session is not managed and its forwards are unknown",
    "specs": "the registered specs of SPEC_KEYS exactly as in backtest/specs.py: entry rules, strikes, wings, DTE "
             "windows, max concurrent, per expiry, days between entries, the IV percentile filter, exits, the "
             "registered same-print fills, slippage tiers, dated charges and expiry settlement",
    "close": "at each session's close every open position of the book is managed by backtest.engine.manage, then "
             "entries are considered spec by spec in SPEC_KEYS order; an accepted position is in the book when the "
             "next spec is considered",
    "trade_size": "every arm: n_trade = min(Book.max_lots, floor(Book.max_loss_pct % of Book.capital / (max loss "
                  "+ entry costs of one lot))), as backtest.engine.try_enter; then the engine's margin rule on the "
                  "whole book: lots fall one at a time while the open positions' entry margins + this trade's "
                  "margin exceed Book.capital; an open position whose margin is unknown refuses every entry",
    "period_end": "entries from PERIOD_START to the period's last session (the last stored session on or before "
                  "PERIOD_END); on that last session no entry is made and every position still open after "
                  "management is squared off at its reference prices with the engine's slippage and charges "
                  "(reason period_end); a run whose stored history ends before the last weekday on or before "
                  "PERIOD_END is labelled partial, not the registered run",
    "equity": "E = Book.capital + realised net P&L + the open positions' (marked gross - entry costs), recorded "
              "with the open risk OR (the open positions' defined max loss at their entry fills, Position.max_loss) "
              "at every session of the period after its entries",
}  # fmt: skip
ARM_RULES = {
    "uncapped": "the trade sizing above and nothing else",
    "budgeted": "primary. Month stop: no entry while E(now) - E(the previous calendar month's last session) "
                "(Book.capital before the first) <= -MONTH_LOSS_STOP_PCT % of capital. Lots = min(real_lots of "
                "fincalc.tradeplan.size(max loss of one lot, its entry costs, Book.max_loss_pct % of capital, "
                "proven=True, cap=Book.max_lots, OpenBook(capital, OR, funds=Funds(cash=capital, margin_used=the "
                "open entry margins, collateral=0, im_one=one lot's margin))), tradeplan.shock_lots(L_book, L_1, "
                "SHOCK_BUDGET_PCT % of capital, moves=PRIMARY_MOVES)); refused when < 1. L(m) is "
                "fincalc.book_scenarios.shock_pnls from this close (15:30 IST) to the next stored session's close: "
                "legs at their marks, the engine's forward (backtest.pricing.forward at Book.rate), every expiry "
                "discounted and rolled at Book.rate (Market.fixed_carry, this study's rule), BANKNIFTY at the worse "
                "end of its registered beta band estimated as of the session from the front-future settles read so "
                "far (fincalc.beta.estimate, no lot adjustment). Any unknown term sizes 0",
    "card_rule": "reported, never gated: the budgeted rule with the shock at every move of tradeplan.SHOCK_MOVES and "
                 "the Kelly ceiling as live cards apply it: 0 lots while mu_lo <= 0 or unknown, mu_lo = the lower "
                 "end of the 95 % expiry-clustered bootstrap CI (backtest.metrics.cluster_bootstrap, "
                 "Criteria.bootstrap_draws and bootstrap_seed) of the net Rs per trade of that spec's uncapped "
                 "trades closed at or before the session, unknown below KELLY_MIN_TRADES of them",
    "control": "computed, not replayed: the uncapped book's daily P&L x k, k = mean OR(budgeted) / mean OR(uncapped) "
               "over every session of the period (fractional lots; costs only approximately proportional); "
               "mean OR(uncapped) = 0: unknown",
}  # fmt: skip
STATISTICS = {
    "months": "monthly P&L = E at a calendar month's last session - E at the previous month's last session "
              "(Book.capital before the first); each month's daily P&L path is kept",
    "worst_month": "W = the minimum monthly P&L (Rs, signed)",
    "drawdown": "D = the largest fall of E from its running peak (Rs; the peak starts at Book.capital), daily",
    "primary": "both must hold: W(budgeted) > W(control) and D(budgeted) < D(control), each by a one-sided moving-"
               "block bootstrap over the M calendar months: blocks of BLOCK_MONTHS consecutive months with starts "
               "uniform over the M - BLOCK_MONTHS + 1 possible starts, ceil(M / BLOCK_MONTHS) blocks concatenated "
               "and cut to M months, the same draw for both books; in each draw W and D are recomputed from the "
               "drawn months' daily paths in their drawn order; dW = W_b - W_c, dD = D_c - D_b; p = the share of "
               "DRAWS draws (SEED) with the difference <= 0; each passes when p < ALPHA. Fewer than MIN_MONTHS "
               "months or k unknown: unknown, which fails",
    "secondary": "1 - entries(budgeted) / entries(uncapped) <= MAX_TRADES_FALL (entries = positions opened; no "
                 "uncapped entry: unknown, fails)",
    "reported": "not gated: the raw cut (W and D of budgeted against uncapped); per arm the entries, closed "
                "trades, expectancy per trade with its 95 % expiry-clustered bootstrap CI as the backtest reports it "
                "(backtest.metrics.trade_stats with Criteria.bootstrap_draws and bootstrap_seed), total net P&L, "
                "mean OR, refusals by reason and lots cut by each binding term; the card rule against uncapped",
}  # fmt: skip
READING = ("Seen data, secondary period: the outcome is reported whatever it is, labelled seen. Nothing is adopted, "
           "no limit or card changes because of it, and it creates no candidate. The registered test of the rule is "
           "the vault study risk_budget_counterfactual (one logged read with the pinned engine after "
           "library_v1_same_print), which this study never performs.")  # fmt: skip


def settings() -> dict:
    """Every numeric setting, read at call time from the module that owns it (so editing one moves the hash)."""
    from finresearch.backtest import dbhistory
    from finresearch.backtest import specs as bspecs
    from finresearch.fincalc import (
        beta_specs,
        book_scenarios,
        fno_history,
        margin_approx,
        tradeplan,
        volatility,
    )

    return {
        "study": {"period": [PERIOD_START, PERIOD_END], "spec_keys": SPEC_KEYS, "fill": FILL,
                  "span_ranges": SPAN_RANGES, "arms": ARMS, "primary_moves": PRIMARY_MOVES,
                  "kelly_min_trades": KELLY_MIN_TRADES, "block_months": BLOCK_MONTHS, "draws": DRAWS, "seed": SEED,
                  "alpha": ALPHA, "max_trades_fall": MAX_TRADES_FALL, "min_months": MIN_MONTHS},
        "limits": {"book_budget_pct": tradeplan.BOOK_BUDGET_PCT, "shock_budget_pct": tradeplan.SHOCK_BUDGET_PCT,
                   "card_shock_moves": tradeplan.SHOCK_MOVES, "margin_util_max": tradeplan.MARGIN_UTIL_MAX,
                   "cash_share_min": tradeplan.CASH_SHARE_MIN, "month_loss_stop_pct": tradeplan.MONTH_LOSS_STOP_PCT},
        # the four specs only (a spec added to the library later leaves this as it is), the book and the criteria
        "library": {"specs": {k: bspecs.spec_hash(bspecs.by_key(k)) for k in SPEC_KEYS},
                    "book": asdict(bspecs.BOOK), "criteria": asdict(bspecs.CRITERIA)},
        "shock": {"down_slope": book_scenarios.DOWN_SLOPE, "up_slope": book_scenarios.UP_SLOPE,
                  "min_vol": book_scenarios.MIN_VOL},
        "beta": beta_specs.registration_hash(),
        "margin": {"params": margin_approx.PARAMS, "moves": margin_approx.MOVES, "extreme": margin_approx.EXTREME,
                   "extreme_weight": margin_approx.EXTREME_WEIGHT, "span_max_age_days": dbhistory.SPAN_MAX_AGE_DAYS},
        "iv_filter": {"window": volatility.WINDOW, "min_days": volatility.MIN_DAYS},
        "futures_roll": {"quarantine_ln": fno_history.QUARANTINE_LN},
    }  # fmt: skip


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def registration() -> dict:
    return {"version": SPEC_VERSION, "study": STUDY_ID, "vault_study": VAULT_STUDY, "vault_file": VAULT_FILE,
            "vault_file_sha256": VAULT_FILE_SHA256, "label": LABEL, "question": QUESTION, "replay": REPLAY,
            "arms": ARM_RULES, "statistics": STATISTICS, "reading": READING, "settings": settings()}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()

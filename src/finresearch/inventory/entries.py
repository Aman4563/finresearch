"""The inventory's entries (#379). Statuses cite the experiment write-ups; nothing is declared validated without them.
Method strings are imported from the code that emits them, so a renamed constant still maps (tests check every
Signal.method in the source maps to an entry: tests/test_model_inventory_methods.py)."""

from __future__ import annotations

from datetime import date

from finresearch.backtest import specs as bspecs
from finresearch.direction.live import METHOD_IMPLIED, METHOD_LOGIT
from finresearch.fnobook.card_forecasts import RN_METHOD
from finresearch.inventory.registry import ModelEntry
from finresearch.signals.ipo import BASE_RATE_METHOD, BLEND_METHOD
from finresearch.signals.ledger import RUN_METHOD
from finresearch.signals.stock import METHOD as STOCK_METHOD
from finresearch.signals.tradeplan import PLAN_METHOD

FNO_TAGS = ("expiry_weekday", "weekly_expiries", "lot_size", "stt")
FNO_RESULTS = "evals/experiments/fno_backtest/RESULTS.md"
NO_ORDERS = "Analysis only; never sizes beyond the profile's F&O budget; nothing is ordered."

_SIGNALS = (
    ModelEntry("fno_strategy_gate", "F&O strategy gate (signal card)", "rule", "A",
               "Check a user's F&O legs for defined risk, liquidity, budget fit and IV context.",
               "A WAIT/AVOID screen before a manual trade; ENTER only with a matched candidate recipe.",
               NO_ORDERS, ("option chain", "bid/ask", "profile F&O budget", "30-day IV history"),
               ("signals/fno.py", "signals/tradeplan.py"), date(2026, 10, 20),
               "Brier score of the real-world PoP on resolved card forecasts (signals/ledger.py)",
               evidence="rule-based: no edge is claimed; calibration of its PoP is pending (fno_pop_calibration)",
               regime_tags=FNO_TAGS, methods=("rule-based risk/cost/fit gate",)),
    ModelEntry("fno_tradeplan_verdict", "F&O trade-plan verdict (ENTER/PAPER/WAIT/AVOID)", "rule", "A",
               "One verdict and size for a trade-plan card from the checks and the matched backtest evidence.",
               "Manual trade planning; real lots only on ENTER with a current candidate backtest.",
               NO_ORDERS + " Stale backtest evidence caps it at WAIT.",
               ("trade-plan card", "latest backtest run", "open book", "month-to-date P&L"),
               ("signals/tradeplan.py", "fincalc/tradeplan.py"), date(2026, 10, 22),
               "Card ledger: realised P&L sign vs the logged PoP (evals/experiments/card_ledger/PREREG.md)",
               evidence="pre-registered card ledger running; no resolved sample yet",
               regime_tags=FNO_TAGS, methods=(PLAN_METHOD,)),
    ModelEntry("fno_card_rn_pop", "F&O card PoP, risk-neutral (benchmark)", "benchmark", "C",
               "The option market's own probability of profit, as a benchmark for the real-world PoP.",
               "Context beside the card's probability; never a call.", "Benchmark only.",
               ("ATM IV",), ("fnobook/card_forecasts.py",), date(2026, 10, 20),
               "Brier score on resolved card forecasts", evidence="benchmark: scored, not validated",
               regime_tags=FNO_TAGS, methods=(RN_METHOD,)),
    ModelEntry("direction_implied", "F&O direction: market-implied lognormal (benchmark)", "benchmark", "C",
               "The option market's probability of an index move over 1-5 sessions.",
               "The benchmark the direction logistic must beat.", "Benchmark only; no edge is claimed.",
               ("30-day IV",), ("direction/live.py",), date(2026, 10, 19),
               "Brier score vs outcomes in the forecast ledger", evidence="benchmark: scored, not validated",
               regime_tags=("expiry_weekday", "weekly_expiries"), methods=(METHOD_IMPLIED,)),
    ModelEntry("direction_logistic", "F&O direction: L2 logistic, walk-forward", "signal", "B",
               "Probability of an index move over 1-5 sessions from returns, IV and calendar features.",
               "Shadow-logged beside the implied benchmark; used only if a registered cell passes.",
               "Shadow only until a pre-registered cell passes.", ("index closes", "30-day IV", "expiry flag"),
               ("direction/",), date(2026, 10, 19), "Brier skill vs the implied benchmark, walk-forward",
               declared="failed", validated_on=date(2026, 10, 19),
               evidence="evals/experiments/fno_direction/RESULTS.md: nothing passed",
               regime_tags=("expiry_weekday", "weekly_expiries"), methods=(METHOD_LOGIT,)),
    ModelEntry("ipo_base_rate", "IPO listing gain: QIB band × regime base rate", "signal", "B",
               "P(listing-day open above the issue price) from comparable past issues.",
               "The IPO card's probability while the fitted model has not passed its bar.",
               "Post-Apr-2022 regime; mainboard issues.", ("final QIB subscription", "issue history"),
               ("signals/ipo.py", "fincalc/ipo.py"), date(2026, 10, 1),
               "Brier score on resolved IPO forecasts (ledger)", evidence="empirical base rate: scored, not validated",
               methods=(BASE_RATE_METHOD, "empirical base rate by final QIB band × regime (")),
    ModelEntry("ipo_blend", "IPO listing gain: shrinkage blend (shadow)", "signal", "B",
               "Blend of the logistic listing model and the base rate.", "Shadow-logged; never the call.",
               "Shadow only (post hoc variant).", ("final-book features", "base rate"), ("signals/ipo.py",),
               date(2026, 10, 1), "Brier skill vs the base-rate table on new issues",
               evidence="evals/experiments/ipo_calibration/SHADOW.md: post hoc, shadow-scored",
               methods=(BLEND_METHOD,)),
    ModelEntry("ipo_logistic", "IPO listing model: L2 logistic + quantile regression", "signal", "B",
               "Listing-gain probability and range from final-book features.",
               "Used for the call only when its walk-forward gate artefact passes.", "Final book only (post cut-off).",
               ("final-book features",), ("evals/ipo_model.py", "signals/ipo.py"), date(2026, 10, 1),
               "Brier skill vs the base-rate table by year", declared="failed", validated_on=date(2026, 10, 1),
               evidence="evals/experiments/ipo_calibration/RESULTS.md: raw model 3/7 years, does not pass",
               methods=("L2 logistic + quantile regression on the final-book features",)),
    ModelEntry("ipo_rules", "IPO apply/skip rules", "rule", "A",
               "Apply or skip from published evidence and the user's rules when no probability is honest.",
               "A manual application decision before the UPI cut-off.", "Personal research; not advice.",
               ("subscription", "profile rules"), ("signals/ipo.py",), date(2026, 10, 1),
               "Listing outcome of applied vs skipped issues (ledger)", evidence="rule-based: not validated",
               methods=("rule: ", "your rules")),
)  # fmt: skip

_OTHER = (
    ModelEntry(
        "stock_composite",
        "Stock composite v1 (momentum, trend, valuation, shareholding)",
        "signal",
        "B",
        "A 12-month tilt for a stock from pre-registered weights.",
        "INFORMATIONAL while no pre-registered stock model has passed.",
        "No BUY/SELL call until one passes.",
        ("price history", "valuation history", "shareholding"),
        ("signals/stock.py",),
        date(2026, 10, 1),
        "Excess return vs the point-in-time equal-weight universe (deflated Sharpe)",
        evidence="evals/experiments/stock_pit_universe/RESULTS.md: no pre-registered variant passed",
        methods=(STOCK_METHOD,),
    ),
    ModelEntry(
        "fund_rules",
        "Mutual fund rules (§D.3)",
        "rule",
        "A",
        "Rank an active fund against its SEBI category on cost, consistency, downside and style.",
        "Manual fund selection.",
        "Not applied to passive funds.",
        ("AMFI NAV", "AMFI TER", "SEBI category"),
        ("signals/fund.py",),
        date(2026, 10, 1),
        "3-year hit rate vs the category median on later windows",
        evidence="rule-based: not validated",
        methods=("§D.3 rules",),
    ),
    ModelEntry(
        "bond_rules",
        "Bond rules (§D.4 expected-loss-adjusted yield)",
        "rule",
        "A",
        "Compare a bond's post-tax, expected-loss-adjusted yield with G-secs and FDs.",
        "Manual bond selection.",
        "Rated corporate and government bonds only.",
        ("bond price", "rating", "FBIL G-sec", "FD rates"),
        ("signals/bond.py",),
        date(2026, 10, 1),
        "Realised vs expected yield and defaults over the horizon",
        evidence="rule-based: not validated",
        methods=("§D.4 expected-loss-adjusted",),
    ),
    ModelEntry(
        "run_verdict",
        "Research report verdict (fixed confidence map)",
        "signal",
        "B",
        "Turn a gate-passed report's verdict into a probability for the ledger.",
        "Scoring the research pipeline's calls.",
        "Uncalibrated map; never a size.",
        ("report verdict",),
        ("signals/ledger.py",),
        date(2026, 10, 1),
        "Brier score of the mapped probability on resolved runs",
        evidence="uncalibrated",
        methods=(RUN_METHOD,),
    ),
    ModelEntry(
        "no_signal",
        "No signal (not enough data)",
        "none",
        "C",
        "Say that no honest signal can be computed.",
        "Shown instead of a call.",
        "Never a call.",
        (),
        ("signals/",),
        date(2026, 10, 1),
        "None: it makes no claim",
        evidence="makes no claim",
        methods=("none: ", "not computed", "insufficient history", "not applied to passive funds"),
    ),
)

_RISK = tuple(
    ModelEntry(k, n, "risk", t, p, "Shown beside F&O and portfolio decisions as risk context.",
               "A model figure, not a forecast; real prices have fat tails.", i, c, d, m,
               evidence="no outcome analysis yet", regime_tags=tags)
    for k, n, t, p, i, c, d, m, tags in (
        ("risk_margin_approx", "F&O margin approximation", "B", "Approximate SPAN + exposure margin of a strategy.",
         ("NSE SPAN ranges", "lot size"), ("fincalc/margin_approx.py",), date(2026, 10, 21),
         "Approximation error vs the broker's margin calculator", ("lot_size",)),
        ("risk_lognormal_pop", "Lognormal probability of profit", "B", "P(P&L > 0 at expiry) under a lognormal law.",
         ("IV", "realised volatility"), ("fincalc/options.py",), date(2026, 10, 20),
         "Brier score of PoP on resolved card forecasts (fno_pop_calibration)", FNO_TAGS),
        ("risk_drawdown_mc", "Drawdown Monte Carlo", "C", "P(running P&L falls a threshold below its peak).",
         ("per-trade P&L distribution",), ("fincalc/options.py",), date(2026, 10, 20),
         "Observed vs simulated drawdown frequency in the paper book", ()),
        ("risk_var", "Portfolio historical VaR / CVaR", "B", "Loss quantiles of the portfolio from history.",
         ("value history",), ("portfolio/analytics.py",), date(2026, 10, 20),
         "VaR exceedance rate vs its level (Kupiec test)", ()),
        ("risk_stress", "Portfolio stress replay", "C", "Replay of past market shocks on today's holdings.",
         ("holdings", "historical shocks"), ("portfolio/stress_replay.py",), date(2026, 10, 20),
         "Reasonableness vs the actual drawdowns of those episodes", ()),
    )
)  # fmt: skip


def _recipe(s) -> ModelEntry:
    tier = "A" if s.role == "candidate" else "C"
    overlay = s.role == "overlay"
    return ModelEntry(f"recipe:{s.key}", s.name, "recipe", tier, s.rationale or s.name,
                      "Library recipe for 'Suggest setups'; real lots only as a current candidate.", NO_ORDERS,
                      ("F&O bhavcopy", "30-day IV", "dated charges"), ("backtest/specs.py", "backtest/engine.py"),
                      date(2026, 10, 12), "Out-of-sample expectancy, clustered t, bootstrap CI (backtest/evaluate.py)",
                      declared="unvalidated" if overlay else "failed", validated_on=None if overlay else date(2026, 10, 12),
                      evidence=(f"{FNO_RESULTS}: not evaluated (no stock options in the history copy)" if overlay
                                else f"{FNO_RESULTS}: nothing passed the pre-registered criteria"),
                      regime_tags=FNO_TAGS)  # fmt: skip


ENTRIES: tuple[ModelEntry, ...] = (*_SIGNALS, *_OTHER, *_RISK, *(_recipe(s) for s in bspecs.SPECS))

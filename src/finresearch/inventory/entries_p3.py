"""Inventory entries for the models merged after the first inventory slice (#379, second slice): the card horse
race and its challengers A2-A6, the volatility race, the VRP studies, betas, joint risk, risk alerts, the VaR
ledger, the skill monitor, the Lab's overfitting checks and the portfolio analytics. Every registration constant is
imported from the code that defines it, so a rename breaks the CI gate (tests/test_model_inventory_methods.py), not
the mapping. None of these is validated: each is pre-registered with no out-of-sample verdict yet, or (Lab rigour)
failed part of its registered check."""

from __future__ import annotations

from datetime import date

from finresearch.fincalc import beta_specs, brinson
from finresearch.horserace import addendum, addendum_a2, addendum_a3, addendum_a4, addendum_a5, addendum_a6
from finresearch.horserace import specs as hspecs
from finresearch.inventory.registry import ModelEntry
from finresearch.optpanel import specs as ospecs
from finresearch.portfolio import (
    factor_exposures,
    joint_risk,
    liquidity,
    optimise_math,
    var_ledger,
)
from finresearch.refseries import tri
from finresearch.skillmon import specs as skspecs
from finresearch.skillmon import specs_v2 as skspecs_v2
from finresearch.volrace import specs as vspecs
from finresearch.vrp import specs as vrpspecs

HR = "evals/experiments/card_horse_race/ADDENDUM.md"
HR_PRE = "evals/experiments/card_horse_race/PREREG.md"
FNO_TAGS = ("expiry_weekday", "weekly_expiries", "lot_size", "stt")
ANALYSIS = "Analysis only: shown beside a decision as context; nothing is ordered or sized from it."
D22 = date(2026, 10, 22)
_HR_STATUS = ("pre-registered challenger; the replay windows (design, seen, tuesday) cannot adopt and the primary "
              "forward window has no resolved sample yet")  # fmt: skip


def _hr(key: str, name: str, purpose: str, version: str, code: tuple[str, ...], inputs: tuple[str, ...],
        challengers: tuple[str, ...]) -> ModelEntry:  # fmt: skip
    return ModelEntry(key, name, "signal", "B", purpose, "Shadow-scored in the card horse race beside the incumbent "
                      "p_rw; replaces it only if it passes the registered adoption gate.",
                      "Shadow only: never the card's probability until adopted.", inputs, code, D22,
                      "Brier score vs p_rw (DM-HLN, Holm) and tail coverage (Kupiec, Berkowitz) in a primary window",
                      evidence=f"{HR} ({version}): {_HR_STATUS}", regime_tags=FNO_TAGS,
                      registrations=(version, *(f"horserace:{c}" for c in challengers)))  # fmt: skip


HORSE_RACE = (
    ModelEntry("horse_race", "Card horse race (adoption gate for PoP models)", "rule", "A",
               "Score every PoP forecaster of the trade-plan card against outcomes and decide adoption.",
               "The only route by which a challenger can replace the card's headline probability.",
               "Replay windows report only; adoption needs a primary window (forward card ledger or the vault).",
               ("card ledger outcomes", "F&O bhavcopy replay"), ("horserace/",), D22,
               "Brier, log score, CRPS, Murphy decomposition; DM-HLN with Holm", evidence=f"{HR_PRE}: registered, "
               "no primary-window result yet", regime_tags=FNO_TAGS,
               registrations=(hspecs.SPEC_VERSION, addendum.ADDENDUM_VERSION)),
    ModelEntry("horse_race_base_rates", "Card horse race: trailing and gate-conditional base rates", "benchmark", "C",
               "Laplace-smoothed base rates of earlier resolved card events (p_base, p_gate).",
               "Benchmarks in the horse race.", "Benchmark only.", ("resolved card events",), ("horserace/race.py",),
               D22, "Brier score in the horse race", evidence="benchmark: scored, not validated",
               registrations=("horserace:p_base", "horserace:p_gate")),
    _hr("hr_p_rw_bus", "PoP challenger A2: business-time clock (p_rw_bus)",
        "The incumbent lognormal PoP with time to expiry measured in business time.", addendum_a2.ADDENDUM_VERSION,
        ("fincalc/bustime.py", "horserace/replay.py"), ("realised volatility", "NSE calendar"),
        (addendum_a2.CHALLENGER,)),
    _hr("hr_p_rn_b76", "PoP challenger A3: parity forward and Black-76 IV (p_rn_b76)",
        "Lognormal PoP at the ATM IV solved on the put-call-parity forward.", addendum_a3.ADDENDUM_VERSION,
        ("fincalc/black76.py", "horserace/replay.py"), ("option settles", "futures"), (addendum_a3.CHALLENGER,)),
    _hr("hr_p_rn_svi", "PoP challenger A4: SVI risk-neutral density (p_rn_svi)",
        "PoP from the SVI-fitted smile's Breeden-Litzenberger density.", addendum_a4.ADDENDUM_VERSION,
        ("fincalc/svi.py", "fincalc/smile.py", "horserace/replay.py"), ("option settles",),
        (addendum_a4.CHALLENGER,)),
    _hr("hr_p_rw_tail", "PoP challengers A5: IV-scaled empirical and FHS (p_rw_emp, p_rw_fhs)",
        "Tail-aware real-world PoP: historical shapes scaled to today's ATM IV.", addendum_a5.ADDENDUM_VERSION,
        ("fincalc/taildist.py", "horserace/replay.py"), ("index history", "ATM IV"),
        (addendum_a5.EMPIRICAL, addendum_a5.FHS)),
    _hr("hr_p_path", "PoP challenger A6: exit-rule Monte Carlo (p_path)",
        "P(P&L > 0) under the plan's own exits (target, stop, DTE exit), not at expiry.", addendum_a6.ADDENDUM_VERSION,
        ("fnobook/path_pop.py", "horserace/replay.py"), ("ATM IV", "exit plan"), (addendum_a6.CHALLENGER,)),
)  # fmt: skip

_X = "evals/experiments"
STUDIES = (
    ModelEntry("vol_race", "Volatility forecast race (log HAR-IV vs the market)", "signal", "B",
               "Forecast an index's realised variance over 1-22 sessions; the race decides whether HAR-IV beats IV.",
               "Read by the vol race report; used by a card only if the registered adoption gate passes.",
               "Study only until adopted.", ("front-future OHLC", "30-day IV"), ("volrace/",), D22,
               "QLIKE loss vs the market benchmark (DM, MCS) per window",
               evidence=f"{_X}/vol_forecast_race/PREREG.md: registered, not run out of sample",
               regime_tags=FNO_TAGS, registrations=(vspecs.SPEC_VERSION,)),
    ModelEntry("vrp_collectable", "Index VRP in the form a trader could collect", "signal", "B",
               "Model-free implied minus realised variance and the short-straddle P&L after dated costs.",
               "Context for option selling; never a call.", "Study only: aggregates by period and era.",
               ("index options", "India VIX", "dated charges"), ("vrp/", "fincalc/mfiv.py"), D22,
               "Newey-West mean of the collectable P&L by era (registered reading rule)",
               evidence=f"{_X}/vrp_collectable/PREREG.md: registered; vault read not done",
               regime_tags=FNO_TAGS, registrations=(vrpspecs.SPEC_VERSION,)),
    ModelEntry("stock_option_vrp", "Cross-sectional stock-option VRP and its panel", "signal", "B",
               "Whether stock options' IV over realised volatility predicts the short-option P&L across stocks.",
               "Research panel only.", "Study only; the panel never reaches a card or a model prompt.",
               ("stock-option bhavcopy", "MWPL", "ban notices"), ("optpanel/",), D22,
               "The registered decision rule on the panel's out-of-sample window",
               evidence=f"{_X}/stock_option_vrp/PREREG.md: registered, not run", regime_tags=FNO_TAGS,
               registrations=(ospecs.SPEC_VERSION,)),
    ModelEntry("book_beta", "Beta to NIFTY of F&O underlyings (EWMA WLS)", "risk", "B",
               "Each underlying's front-future beta to NIFTY with a conservative band.",
               "Sizes the book's scenarios and the joint risk view's common shock.", "A model figure, not a forecast.",
               ("front-future settles",), ("fincalc/beta.py", "fincalc/beta_specs.py"), D22,
               "Out-of-sample hedge error of the beta (book_betas PREREG)",
               evidence=f"{_X}/book_betas/PREREG.md: registered, no outcome analysis yet", regime_tags=("lot_size",),
               registrations=(beta_specs.SPEC_VERSION,)),
    ModelEntry("joint_risk", "Joint risk view: portfolio and F&O book", "risk", "B",
               "One NIFTY-delta and scenario P&L across holdings (by beta) and the F&O book (full revaluation).",
               "Risk context on the joint risk page.", ANALYSIS, ("holdings and betas", "F&O legs", "option chains"),
               ("fincalc/joint_risk.py", "portfolio/joint_risk.py", "fincalc/book_scenarios.py"), D22,
               "R² of explained vs actual daily joint P&L on big-move days (joint_risk forward test)",
               evidence=f"{_X}/joint_risk/PREREG.md: forward test registered, no verdict yet",
               methods=(joint_risk.METHOD,)),
    ModelEntry("risk_alerts", "Risk alerts (limits, IV shocks, gaps, touch, events)", "rule", "B",
               "Fire alerts when a limit's utilisation, an IV shock, a gap or a touch probability crosses its level.",
               "Prompts a manual review; never an action by itself.", ANALYSIS,
               ("stored IV and closes", "limits", "F&O book"), ("fincalc/risk_alerts.py", "alerts/risk.py"), D22,
               "Alert precision (actioned / fired) per category", evidence="rule-based: precision not yet measured",
               regime_tags=FNO_TAGS),
    ModelEntry("risk_var_ledger", "Portfolio 1-day 95 % VaR (forecast ledger)", "risk", "B",
               "The next day's loss quantile from the last 250 daily returns, logged before the outcome.",
               "Risk context; backtested on its own exceedances.", "A model figure, not a forecast of returns.",
               ("value history",), ("portfolio/var_ledger.py",), D22,
               "Kupiec and Christoffersen tests once 250 days are logged",
               evidence="ledger running; fewer than 250 logged days", methods=(var_ledger.METHOD,)),
    ModelEntry("skill_monitor", "Live forecast skill monitor", "rule", "B",
               "Calibration-drift and skill alarms for every forecast-ledger cell.",
               "Flags a model whose live forecasts drift; never promotes one.", "Monitoring only.",
               ("forecast ledger", "stored F&O history", "VaR ledger", "IPO history"), ("skillmon/",),
               date(2026, 10, 26), "Simulated false-alarm rate (registered thresholds)",
               evidence=f"{_X}/skill_monitoring/PREREG.md and ADDENDUM.md (v2): registered; thresholds from "
                        "synthetic simulation",
               registrations=(skspecs.SPEC_VERSION, skspecs_v2.SPEC_VERSION)),
    ModelEntry("lab_overfit", "Strategy Lab overfitting checks (PBO by CSCV, Hansen SPA)", "rule", "B",
               "Probability of backtest overfitting and a data-snooping test over a user's Lab trials.",
               "Shown beside a Lab result.", "A screen, not a proof of edge.", ("Lab trial returns",),
               ("stratlab/overfit.py",), D22, "Size and power on registered synthetic panels",
               declared="failed", validated_on=D22,
               evidence=f"{_X}/lab_rigour/RESULTS.md: items B (SPA size 8 %) and C (power) fail"),
)  # fmt: skip

ANALYTICS = (
    ModelEntry("factor_exposures", "Factor exposures (IIMA four-factor OLS)", "analytics", "C",
               "Market, size, value and momentum betas of the portfolio and each holding.",
               "Describes the portfolio's tilts.", ANALYSIS, ("value history", "IIMA factor returns"),
               ("portfolio/factor_exposures.py",), date(2026, 10, 20), "Out-of-sample R² of the fitted exposures",
               evidence="descriptive: Newey-West standard errors, not validated", methods=(factor_exposures.METHOD,)),
    ModelEntry("brinson_attribution", "Brinson-Fachler attribution vs the Nifty 50", "analytics", "C",
               "Allocation, selection and interaction by sector of today's holdings against the Nifty 50.",
               "Describes where a gap to the index came from.", ANALYSIS, ("holdings", "Nifty 50 weights", "closes"),
               ("fincalc/brinson.py", "portfolio/attribution.py"), D22, "The parts sum to the active return",
               evidence="identity-checked arithmetic: not a forecast", methods=(brinson.METHOD,)),
    ModelEntry("liquidity_days", "Days to liquidate a holding", "analytics", "C",
               "Units over a participation share of the 20-session average traded volume.",
               "Context for position size.", ANALYSIS, ("equity bhavcopy volumes",), ("portfolio/liquidity.py",), D22,
               "Reasonableness vs actual exit times", evidence="rule-based: not validated", methods=(liquidity.METHOD,)),
    ModelEntry("optimiser", "Portfolio optimiser (min variance, risk parity, HRP) with a tax-aware trade list",
               "analytics", "B", "Target weights from the covariance of holdings and the trades (after tax) to reach them.",
               "Illustrative trade list for a manual decision; not advice.",
               ANALYSIS + " Illustrative only; brokers are read-only.", ("value history", "lots and tax"),
               ("portfolio/optimise.py", "portfolio/optimise_math.py"), date(2026, 10, 21),
               "Walk-forward realised volatility vs the current weights", evidence="walk-forward shown, not validated",
               registrations=tuple(f"optimise_math:{m}" for m in optimise_math.METHODS)),
    ModelEntry("goal_planner", "Goal planner (seeded Monte Carlo of SIPs and inflation)", "analytics", "B",
               "P(reaching a goal) and the SIP needed, from stated return and CPI inflation assumptions.",
               "Planning aid; the assumptions are editable.", "Stated assumptions, not a forecast.",
               ("goal", "SIP", "asset-class assumptions", "MoSPI CPI"), ("wealth/goals.py", "wealth/inflation.py"),
               date(2026, 10, 21), "None yet: outcomes are years away", evidence="assumption-driven: not validated"),
    ModelEntry("tri_approx", "Approximate total-return index", "analytics", "C",
               "A total-return series from NSE's daily close and dividend yield where the official TRI is missing.",
               "Benchmarks on the portfolio pages, labelled approximate.", "Approximate: never the official TRI.",
               ("index close", "dividend yield"), ("refseries/tri.py",), D22,
               "Tracking difference vs the official TRI where both exist", evidence="labelled approximate",
               methods=(tri.METHOD,)),
)  # fmt: skip

ENTRIES_P3: tuple[ModelEntry, ...] = (*HORSE_RACE, *STUDIES, *ANALYTICS)

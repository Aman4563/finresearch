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
from finresearch.portfolio import factor_exposures, joint_risk, liquidity, optimise_math, stress_replay, var_ledger
from finresearch.refseries import tri
from finresearch.skillmon import specs as skspecs
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

"""The second registration of the live skill monitor (P2.3, #378): feature drift (PSI) of the direction model's and
the F&O card's inputs, the IPO quantile range's pinball loss and 80 % coverage with issue #378's acceptance (d), and
the Kupiec trigger that marks a risk model "not for limits" in the model inventory. Hashed by `registration_hash()`
and pinned in `prereg.V2_HASH` before any code read a stored input, outcome or IPO row for it
(evals/experiments/skill_monitoring/ADDENDUM.md, post hoc to PREREG.md).

`skill-monitor-v1` (skillmon/specs.py) is unchanged and still governs every forecast cell; this version wires three
items v1 registered for later (`specs.Later.psi`, `.pinball`, `.risk_triggers`) and fixes their open choices. Never
edit a registered value in place: a change is a new SPEC_VERSION recorded in ADDENDUM.md, with its hash committed
before the changed monitor reads anything. Nothing here changes a call, sizes a trade or promotes a model.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date

from finresearch.direction import specs as dspecs
from finresearch.evals import ipo_model as im
from finresearch.horserace import specs as hspecs
from finresearch.portfolio import var_ledger
from finresearch.skillmon import specs

SPEC_VERSION = "skill-monitor-v2"
BASE_VERSION = specs.SPEC_VERSION  # v1: its cells, thresholds and hash are unchanged


@dataclass(frozen=True)
class Drift:
    statistic: str = (
        "PSI = Σ_b (a_b - e_b) ln(a_b / e_b) over the bins' shares, each floored at eps (stats.psi)"
    )
    bins: str = ("edges = the distinct values among the reference sample's 10th, 20th, ..., 90th percentiles (numpy "
                 "linear interpolation); bins right-closed (-inf, e1], (e1, e2], ..., (ek, +inf): at most 10, and a "
                 "0/1 flag's values fall in 2 of them")  # fmt: skip
    eps: float = 1e-4
    watch: float = 0.10  # PSI >= watch: on watch (issue #378)
    act: float = 0.25  # PSI >= act: act, an alarm (issue #378)
    reference_min: int = (
        250  # known values of the input inside the reference window; fewer: insufficient data
    )
    live_values: int = (
        250  # the input's last known values after the reference window; fewer: insufficient data
    )
    live: str = ("the input's last live_values known values on the panel's stored sessions after the reference "
                 "window's end (every stored session, not only the days a forecast was logged)")  # fmt: skip
    alarm: str = ("per model and underlying: one alarm when any input reads act, naming those inputs by PSI, largest "
                  "first; one info line counts the inputs on watch and one the inputs with insufficient data")  # fmt: skip
    known_limit: str = ("under an i.i.d. no-drift null PSI is about (1/n_live + 1/n_ref) chi2(bins - 1) (Yurdakul "
                        "2018 [U]): simulated with 600 reference and 250 live values in 10 bins its mean is 0.052, "
                        "4.2 % of draws reach 0.10 and none of 4,000 reaches 0.25; serially correlated inputs (long "
                        "returns, moving averages, volatilities) hold fewer independent values and cross both levels "
                        "more often without any break")  # fmt: skip


DRIFT_STATUSES = {
    "act": "PSI >= act: an alarm",
    "watch": "watch <= PSI < act: shown, not an alarm",
    "ok": "PSI < watch",
    "insufficient data": "fewer than reference_min reference or live_values live known values: never read as ok",
    "unknown": "the underlying's panel could not be read (the error is kept): never read as ok",
}
PANEL = ("direction.data.load(symbol): the app's own stored F&O history, read only; the inputs by "
         "direction.features.build_features on it, one panel per underlying for both models")  # fmt: skip
# The stored daily series behind the card's two PoPs and its recipe IV filter (signals/fno.py `analyse`,
# signals/tradeplan.py's iv_pct_min filter). The live card reads its chain's ATM IV and NSE's closes, not this panel.
CARD_INPUTS = {
    "iv_30": "30-day constant-maturity ATM IV, annual % (the series the risk-neutral PoP and the IV percentile track)",
    "rv_20": "20-session realised volatility of the stored returns, annual % (the sigma of the card's p_rw20)",
    "rv_60": "60-session realised volatility of the stored returns, annual % (the sigma of the card's p_rw60)",
    "iv_pctile": "fincalc.volatility.iv_stats_sessions percentile of iv_30 over its trailing 252 sessions (the "
                 "recipes' iv_pct_min filter)",
    "iv_rv_ratio": "iv_30 / rv_20 (the card's IV/RV ratio)",
}  # fmt: skip
_DESIGN = hspecs.WINDOWS["design"]
MODELS = {
    "direction": {"inputs": tuple(dspecs.FEATURES), "underlyings": dspecs.UNDERLYINGS,
                  "reference": (dspecs.TRAIN_START, dspecs.IS_END),
                  "why": "the registered training window (direction/specs.py TRAIN_START..IS_END), the in-sample years "
                         "of the walk-forward its acceptance test used; the live logistic refits monthly on an "
                         "expanding window, so this is drift from the registration's training data, not from the "
                         "latest refit"},
    "card": {"inputs": tuple(CARD_INPUTS), "underlyings": hspecs.UNDERLYINGS,
             "reference": (date.fromisoformat(_DESIGN["start"]), date.fromisoformat(_DESIGN["end"])),
             "why": "the card's probabilities are registered in the card horse race (card-horse-race-v1, incumbent "
                    "p_rw), and this is its design window; the recipes' in-sample window (backtest/specs.py) starts "
                    "three months earlier, on 2022-01-01"},
}  # fmt: skip


@dataclass(frozen=True)
class IpoIntervals:
    walk_forward: str = ("evals.ipo_model's split, unchanged: its usable() mainboard rows, train on listing years < t "
                         "and test year t for t in its TEST_YEARS, fitted by its fit(), predict() and "
                         "table_forecaster(); a year with fewer than 30 training rows or no test issue is skipped by "
                         "it and reads insufficient data")  # fmt: skip
    forecasters: tuple[str, ...] = ("model", "table")
    model: str = (
        "the L2 quantile regression's q10, q50, q90 per test issue (evals.ipo_model.predict, rearranged)"
    )
    table: str = ("the QIB band x regime base-rate cell's empirical 10/50/90 % returns (table_forecaster): the range "
                  "the IPO card shows while the model has not passed its gate")  # fmt: skip
    outcome: str = "return_open: the listing-day open over the issue price minus 1, not winsorised"
    taus: tuple[float, ...] = tuple(im.TAUS)
    pinball: str = (
        "per year and tau, the mean of (y - q)(tau - 1{y < q}) over its test issues (stats.pinball)"
    )
    coverage: str = ("inside = the test issues with q10 <= y <= q90; its share and the exact two-sided binomial p "
                     "against 80 % (horserace.scores.band_coverage)")  # fmt: skip
    nominal_pct: int = 80
    tolerance_pp: int = 7
    within: str = (
        "|100 x inside - nominal_pct x n| <= tolerance_pp x n in integers: 73 of 100 is within, 72 is not"
    )
    years: tuple[int, ...] = tuple(
        im.GATE_YEARS
    )  # the 7 complete years; the partial year is reported, never counted
    pass_years: int = 5
    acceptance: str = ("(d), per forecaster: pass with >= pass_years years within; fail when the years within plus "
                       "the years with insufficient data are fewer than pass_years; else insufficient data")  # fmt: skip
    known_limit: str = ("a perfectly calibrated 80 % range lands within +/-7 pp with probability 0.60 at 20 test "
                        "issues, 0.75 at 30, 0.79 at 50 and 0.94 at 100 (exact binomial, not monotone in n), so a "
                        "small year can miss without any miscalibration")  # fmt: skip
    seen: str = ("evals/artefacts/ipo_model_walkforward.json (committed 30-Sep-2026) already holds the model's "
                 "per-year p10-p90 coverage and pinball from this walk-forward; it was not opened for this "
                 "registration, whose numbers are the issue's (80 %, +/-7 pp, 5 of 7)")  # fmt: skip


@dataclass(frozen=True)
class RiskTrigger:
    test: str = (
        "Kupiec (1995) proportion of failures per quarter, two-sided chi2(1) (horserace.scores.kupiec)"
    )
    alpha: float = 0.05  # a tested quarter fails at p < alpha (issue #378)
    consecutive: int = 3  # failing quarters in a row mark the model (issue #378)
    min_days: int = 50  # resolved days for a quarter's test, about 80 % of its ~62 NSE sessions
    quarter: str = ("the calendar quarter (Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec) of each forecast's own day "
                    "(inputs.as_of, an IST date), one outcome per day (the newest row if two); a quarter is tested "
                    "only once it ended before today (IST) and none of its rows is still open: until then it is "
                    "pending, never a pass, so a tested quarter's result is final")  # fmt: skip
    record: str = ("from the first quarter with a logged row to the last complete quarter; a quarter with fewer than "
                   "min_days resolved days, or none, is insufficient data; void rows never count (an unknown return "
                   "is not 'no exception')")  # fmt: skip
    mark: str = ("'not for limits' once the record holds `consecutive` failing quarters in a row; it stays (no "
                 "reset: only a new version can lift it)")  # fmt: skip
    known_limit: str = ("at 62 days and a 5 % rate a quarter fails with 0 or >= 7 exceptions: 7.6 % of quarters "
                        "under a correct VaR (three in a row about 4e-4) and 43 % when the true rate is 10 % (three "
                        "in a row about 8 %); two-sided, so three quarters without an exception also mark the model")  # fmt: skip


RISK_STATUSES = {
    "not for limits": "`consecutive` failing quarters in a row somewhere in the record (it stays)",
    "not triggered": "at least `consecutive` quarters tested and every run of `consecutive` quarters holds a pass",
    "unknown": ("no failing run, but a run of `consecutive` quarters without a pass includes an untested or "
                "pending quarter"),
    "insufficient data": "fewer than `consecutive` quarters tested: never read as ok",
}  # fmt: skip
RISK_MODELS = {
    "risk_var_ledger": (f"the forecast ledger's {var_ledger.ASSET}/{var_ledger.EVENT_KIND} rows: the 1-day "
                        f"{var_ledger.LEVEL:.0%} historical VaR over {var_ledger.WINDOW} returns, logged before the "
                        "outcome; outcome 1 = an exception; nominal exception rate 1 - LEVEL"),
    "risk_var": ("the same estimator as the ledger (portfolio.analytics VAR_WINDOW 250 at 95 %), so it is scored on "
                 "the ledger's logged figures, never on its walk-forward re-estimate, which a backdated import "
                 "rewrites"),
}  # fmt: skip
BRIEF = ("drift: a warn line (an alarm) per model and underlying with an input at act, one info line counting the "
         "inputs on watch and one counting those with insufficient data; risk: a warn line (an alarm) per model "
         "marked 'not for limits' and an info line per model with insufficient data or unknown; a section that "
         "could not run reads unknown with its error, never ok, and a report from before v2 says both are unchecked")  # fmt: skip
WHERE = ("drift and the risk marks in the nightly skill pass (monitor/skill.py, the report's 'v2' key) and on read in "
         "`finresearch forecasts skill`; the marks also on read in the inventory (GET /api/models 'limits', "
         "`finresearch models list`); the IPO ranges only on read: `finresearch forecasts ipo-intervals`")  # fmt: skip
DRIFT, IPO, RISK = Drift(), IpoIntervals(), RiskTrigger()


def registration() -> dict:
    return {"version": SPEC_VERSION, "base_version": BASE_VERSION, "drift": asdict(DRIFT),
            "drift_statuses": DRIFT_STATUSES, "panel": PANEL, "card_inputs": CARD_INPUTS, "models": MODELS,
            "ipo": asdict(IPO), "risk": asdict(RISK), "risk_statuses": RISK_STATUSES, "risk_models": RISK_MODELS,
            "brief": BRIEF, "where": WHERE}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(specs.canonical(registration()).encode()).hexdigest()

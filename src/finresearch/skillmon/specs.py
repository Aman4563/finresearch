"""The registration of the live skill monitor (P2.3, #378): cells, benchmarks, the trigger, every threshold and
the alarm rules, hashed by `registration_hash()` and pinned in `prereg.py` before any statistic was computed on a
real outcome (evals/experiments/skill_monitoring/PREREG.md).

Never edit a registered value in place: a change is a new SPEC_VERSION recorded as post hoc in the experiment's
ADDENDUM.md (never PREREG.md), with its new hash committed before the changed monitor reads an outcome. Nothing
here promotes a model or changes a call: the promotion statistic is reported for the owner's decision
(direction/specs.py LIVE_PROMOTION).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

SPEC_VERSION = "skill-monitor-v1"

# A cell is (method group, event kind, underlying). The underlying is the instrument for F&O forecasts and "*" (all
# instruments pooled) for every other asset: one IPO lists once, so a per-issue cell would never hold two events.
CELL = "(signals.ledger._method_group(method), event_kind, instrument if asset == 'fno' else '*')"
# Benchmarks, by model method -> benchmark method, paired on the same event (`PAIRING`). A cell whose method is not
# here, or is itself a benchmark, has no skill score ("no benchmark"), never 0.
BENCHMARKS = {
    "direction.live.METHOD_LOGIT": "direction.live.METHOD_IMPLIED",
    "signals.tradeplan.PLAN_METHOD": "fnobook.card_forecasts.RN_METHOD",
    "signals.ipo.BLEND_METHOD": "signals.ipo.BASE_RATE_METHOD",
    "signals.ledger.RUN_METHOD (asset ipo)": "signals.ipo.BASE_RATE_METHOD",
}
PAIRING = ("same asset, instrument and event kind, and the same origin: F&O cards by (source, inputs.card_index); "
           "rows with inputs.start_date by it (direction: also inputs.h and inputs.event, via the event kind); "
           "others by the IST day the forecast was logged. A model row without a resolved benchmark row is unpaired "
           "(counted, never scored against 0.5).")  # fmt: skip
INDEPENDENCE = ("signals.ledger.independent_events with the IPO close dates (the track record's rule): window "
                "events non-overlapping per cluster, the decision-time forecast per IPO, the first card per "
                "underlying, expiry and structure")  # fmt: skip
ORDER = "independent events in the order their outcomes were known (resolved_at, then id)"


@dataclass(frozen=True)
class Trigger:
    # MF16: below it only calibration-in-the-large and the promotion BSS CI are computed (on read and nightly)
    full_events: int = 100  # resolved independent events in the cell
    real_money_cells: tuple[
        str, ...
    ] = ()  # cells that drive real-money size (none: no live model is accepted)
    promotion_events: int = 50  # direction/specs.py LIVE_PROMOTION: independent resolved events per cell


@dataclass(frozen=True)
class Skill:
    statistic: str = "BSS = 1 - Σ(p_m - o)² / Σ(p_b - o)², paired rows in origin order"
    rows: str = ("window events (inputs carry h): every paired resolved row, overlapping, block >= h; every "
                 "other cell: the paired independent events, h = 1")  # fmt: skip
    bootstrap: str = ("moving-block, blocks start uniformly in [0, n - L], ⌈n/L⌉ joined and cut to n; CI = BSS ± "
                      "z(0.975) x the sd of the resampled BSS (the percentile interval is reported beside it)")  # fmt: skip
    known_limit: str = ("covers about 93-94 % (not 95 %) at n = 100-1,000 with overlapping outcomes, and about 90 % "
                        "when the forecast signal is strongly persistent (AR(1) 0.9): the CI is somewhat narrow")  # fmt: skip
    block: str = "L = max(h, ceil(n^(1/3)))"
    reps: int = 2000
    level: float = 0.95
    seed: int = 378
    promotion: str = ("reported as 'CI above 0' only with >= promotion_events independent events and the CI's lower "
                      "end > 0; never automatic")  # fmt: skip
    alarm: str = "full mode: the CI's upper end < 0 (the model is worse than its benchmark)"


@dataclass(frozen=True)
class Calibration:
    citl: str = "mean(o) - mean(p), descriptive"
    spiegelhalter_alarm_p: float = 0.01  # two-sided; alarm below it (full mode)
    murphy_bins: int = 10  # horserace.scores.murphy, descriptive
    cusum_r_up: float = 2.0  # odds doubled
    cusum_r_down: float = 0.5  # odds halved
    cusum_target_arl: int = 500  # in-control events to a false alarm, both charts together
    # set by simulation (skillmon.stats.calibrate_two_sided): p drawn i.i.d. from linspace(0.2, 0.8, 601),
    # o ~ Bernoulli(p), 2,000 paths of 5,000 events, seed 378, both charts on the same paths
    cusum_h_up: float = 3.7625
    cusum_h_down: float = 3.7629
    cusum_reference: str = "p ~ U(0.2, 0.8) i.i.d.; the h are fixed, not re-fitted per cell"
    cusum_reset: str = (
        "none: the chart runs over the cell's whole record, so an alarm stays until a new version"
    )


@dataclass(frozen=True)
class Later:
    """Registered here before they are wired, in a later SPEC_VERSION each: not computed by v1."""

    psi: str = "PSI on decile bins of each logistic feature, live vs the training window: 0.1 watch, 0.25 act"
    pinball: str = "IPO quantile pinball loss at tau 0.1, 0.5, 0.9; p10-p90 coverage against 80 %"
    conformal: str = (
        "adaptive conformal alpha_{t+1} = alpha_t + gamma (alpha - err_t), gamma 0.005, alpha 0.2"
    )
    regimes: str = ("IV30 percentile tercile; trend sign vs the 200-session SMA; crisis = IV30 above the 90th "
                    "percentile of the trailing 3 years; cluster-bootstrap heterogeneity; holds only if on the vault "
                    "and 2022-26")  # fmt: skip
    pnl_cusum: str = (
        "S_t = max(0, S_t-1 + (mu0 - x_t) - k) on forward trade P&L vs the backtest expectancy, ARL 250"
    )
    risk_triggers: str = "Kupiec p < 0.05 in 3 consecutive quarters marks a risk model 'not for limits'"


STATUSES = {
    "insufficient data": "fewer resolved independent events than the trigger: never read as ok",
    "ok": "full mode, every check computed, no alarm",
    "alarm": "full mode, at least one alarm",
    "unknown": "full mode but a check could not be computed (e.g. zero variance)",
}
TRIGGER, SKILL, CALIBRATION, LATER = Trigger(), Skill(), Calibration(), Later()


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def registration() -> dict:
    return {"version": SPEC_VERSION, "cell": CELL, "benchmarks": BENCHMARKS, "pairing": PAIRING,
            "independence": INDEPENDENCE, "order": ORDER, "trigger": asdict(TRIGGER), "skill": asdict(SKILL),
            "calibration": asdict(CALIBRATION), "later": asdict(LATER), "statuses": STATUSES}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()

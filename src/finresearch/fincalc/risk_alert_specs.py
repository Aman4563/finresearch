"""The registration record of the risk alerts' touch-probability formula (#398, #520): each variant's function,
formula, label, level, monitoring assumption, unknown rule and hand-derived reference values, plus the definition of
the replay that compares them, hashed by `registration_hash()` and pinned in `risk_alert_prereg.py`.

No hash was pinned when the alerts shipped: fincalc/risk_alerts.py's docstring says its values "are registered here"
and PR #453 (merge commit 75df78f8, 22-Oct-2026) lists TOUCH_LEVEL under "needs decision"; that code and that PR are
variant 1's only earlier record. This module creates the record post hoc (23-Nov-2026): variant 1 is the formula,
label and level exactly as shipped (read from risk_alerts.py, so an edit there moves the hash), variant 2 the exact
first-passage probability. Prose and the reasons: evals/experiments/risk_alerts/ADDENDUM.md.

Never edit a variant in place: a change is a new variant with a new SPEC_VERSION and a bumped VARIANTS_TRIED, recorded
as post hoc in the ADDENDUM, with its hash committed and pushed before the changed formula is replayed on stored
marks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from finresearch.fincalc import risk_alerts as ra

SPEC_VERSION = "risk-alert-touch-v2"
VARIANTS_TRIED = 2  # 1 = the 2 x P_Q shortcut as shipped (#398); 2 = the exact first passage (#520)
MARK_RATE = 0.065  # alerts.risk_readers.MARK_RATE: the rate fno_mark's IVs were implied with


@dataclass(frozen=True)
class TouchVariant:
    key: str
    function: str
    formula: str
    label: str  # the text every touch reading carries
    level: float  # the alert fires at p >= level
    level_meaning: str
    monitoring: str
    unknown: str
    # (ln(K / F), sigma, T in years, value to 5 decimals): worked by hand at sigma sqrt(T) = 0.2, where the strike
    # below has d1 = 0.6, d2 = 0.4 and the strike above d1 = -0.4, d2 = -0.6, from N(-0.4) = 0.3445783,
    # N(-0.6) = 0.2742531, e^0.1 = 1.1051709, e^-0.1 = 0.9048374
    reference: tuple[tuple[float, float, float, float], ...]
    source: str
    registered: str


V1 = TouchVariant(
    key=ra.TOUCH_V1,
    function="finresearch.fincalc.risk_alerts.touch_prob",
    formula="min(1, 2 N(-d2)) for K < F, min(1, 2 N(d2)) for K > F, d2 = (ln(F / K) - sigma^2 T / 2) / (sigma sqrt(T))",
    label=ra.TOUCH_LABEL,
    level=ra.TOUCH_LEVEL,
    level_meaning="twice the risk-neutral probability of expiring beyond the strike: N(-/+d2) = 0.30 on both sides",
    monitoring="none stated: twice the terminal probability (the reflection principle without the drift term)",
    unknown="t <= 0, sigma <= 0 or K = F return 1.0; nothing returns unknown (the reader guards missing inputs)",
    reference=((-0.1, 0.2, 1.0, 0.68916), (0.1, 0.2, 1.0, 0.54851)),
    source="issue #398's reviewed text of 20-Oct-2026, 'Maths' (driftless reflection)",
    registered="shipped by PR #453 (merge commit 75df78f8, 22-Oct-2026) with no hash; pinned post hoc on 23-Nov-2026 "
               "from the unedited code",
)  # fmt: skip
V2 = TouchVariant(
    key=ra.TOUCH_V2,
    function="finresearch.fincalc.risk_alerts.touch_prob_exact",
    formula="N(-d2) + (F / K) N(-d1) for K < F, N(d2) + (F / K) N(d1) for K > F, 1 at K = F; "
            "d1 = (ln(F / K) + sigma^2 T / 2) / (sigma sqrt(T)), d2 = d1 - sigma sqrt(T)",
    label=ra.TOUCH_LABEL_EXACT,
    level=ra.TOUCH_LEVEL,
    level_meaning="0.60 kept from variant 1, not re-centred (the owner's decision): it is no longer one terminal "
                  "probability; the strike's N(-/+d2) and delta at p = 0.60 move with sigma sqrt(T) (ADDENDUM.md)",
    monitoring="continuous; the alert reads daily closes, which see fewer touches, so the figure is the larger of the "
               "two. The Broadie-Glasserman-Kou barrier shift (monitoring_dates) is off; on would be a new variant",
    unknown="a missing, non-finite or non-positive F, K, sigma or T is unknown (None; the alert reads can't check), "
            "never 0 or 1",
    reference=((-0.1, 0.2, 1.0, 0.64767), (0.1, 0.2, 1.0, 0.58604)),
    source="first passage of a Brownian motion with drift -sigma^2 / 2 (reflection principle with drift); the "
           "one-touch at zero rates, Reiner and Rubinstein (1991), 'Unscrambling the binary code', Risk 4(9)",
    registered="post hoc on 23-Nov-2026 (#520), after the oracle suite (#511) measured variant 1 against QuantLib on "
               "synthetic inputs; before any code that replays stored marks existed",
)  # fmt: skip
VARIANTS = (V1, V2)
ACTIVE = ra.TOUCH_VARIANT

# The replay that compares the two variants on the stored marks (`finresearch fno risk-replay --touch`). It decides
# nothing: descriptive counts for the owner's decision on the level.
REPLAY = {
    "series": "fno_mark rows of short option legs (right call or put, qty < 0 or unconfirmed), paper and Groww, on "
              "every stored session in the asked range; local and read-only; the book is personal, so only counts "
              "are printed",
    "inputs": "as the nightly alert reads them (alerts.risk_readers.leg_readings): forward = spot x exp(MARK_RATE x "
              "t_years), the leg's own stored iv, t_years and strike; both variants on the same inputs",
    "known leg-day": "one such row whose direction is confirmed short and whose iv, spot, t_years and strike are "
                     "stored and positive; any other row is an unknown leg-day: counted, never 'below the level'",
    "at the level": "p >= level (0.60), per variant",
    "leg-days that change": "known leg-days at the level under exactly one variant, split by the strike's side of "
                            "the forward and by which variant",
    "alert-day": "a session with at least one known leg-day at the level under the variant; a session with only "
                 "unknown leg-days is counted as unknown, not as no alert; alert-days that change are sessions that "
                 "are an alert-day under exactly one variant",
    "firings": "per leg (symbol, expiry, strike, right), rows in session order, as alerts.risk.apply: a known reading "
               "at the level fires unless the leg's last reading was also at the level; a reading below the level "
               "or an unknown one ends the standing state",
    "criterion": "none; no count changes the formula or the level without a new registered variant",
}  # fmt: skip


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def variant_hash(v: TouchVariant) -> str:
    return hashlib.sha256(canonical(asdict(v)).encode()).hexdigest()


def registration() -> dict:
    return {"version": SPEC_VERSION, "variants_tried": VARIANTS_TRIED, "active": ACTIVE, "mark_rate": MARK_RATE,
            "variants": [asdict(v) for v in VARIANTS], "replay": REPLAY}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()

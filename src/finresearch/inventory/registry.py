"""The model inventory (#379): one typed entry per model or rule, after the Fed/OCC SR 11-7 model-risk guidance
(purpose, intended use and restrictions, inputs, validation, outcome analysis), sized by a materiality tier.

Status is computed, never asserted: an entry reads "validated" only while it has a validation date and a
`valid_until` on or after today; expired or missing evidence reads "unvalidated" with the reason, never fine.
"failed" stays failed (a model that failed its pre-registered test is not rescued by time).

Materiality tiers (how deep validation must be before the model may drive anything):
- A: drives a real-money action or size (ENTER, APPLY, BUY, lots). Needs a pre-registered out-of-sample test, a
  regime-split re-run and a red-team review of the PREREG and the code by a separate Claude session.
- B: a probability, ranking or call shown beside a decision. Needs an out-of-sample calibration (Brier, reliability).
- C: context, a benchmark or no call. Needs reasonableness checks and a stated outcome metric.

Limits of use, for every entry: personal research only, no distribution, no automated orders (LIMITS_OF_USE).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Tier = Literal["A", "B", "C"]
Status = Literal["unvalidated", "validated", "failed"]
Kind = Literal["signal", "rule", "recipe", "benchmark", "risk", "none"]

LIMITS_OF_USE = (
    "Personal research only: no distribution and no automated orders (nothing here places an order)."
)
TIER_DEPTH: dict[str, str] = {
    "A": "pre-registered out-of-sample test + regime-split re-run + red-team review of the PREREG and code",
    "B": "out-of-sample calibration (Brier score, reliability) on resolved forecasts",
    "C": "reasonableness checks and a stated outcome metric",
}


@dataclass(frozen=True)
class ModelEntry:
    key: str
    name: str
    kind: Kind
    tier: Tier
    purpose: str
    intended_use: str
    restrictions: str
    inputs: tuple[str, ...]
    code: tuple[str, ...]  # modules that implement it
    last_update: date  # when its method or code last changed materially (the issue or PR that did it)
    outcome_metric: str  # what is measured against outcomes, and where
    declared: Status = "unvalidated"  # what the evidence said when it was last judged
    validated_on: date | None = None
    valid_until: date | None = None  # validated evidence expires here
    evidence: str = ""  # where the validation is written up
    regime_tags: tuple[str, ...] = ()  # inventory.regimes tags its evidence depends on
    methods: tuple[str, ...] = ()  # Signal.method values (exact, or a literal prefix of a dynamic one)
    red_team: tuple[str, ...] = field(
        default_factory=tuple
    )  # filed reviews (path or issue), with the PREREG's date

    def status(self, today: date) -> tuple[Status, str]:
        """(status, reason) on `today`."""
        if self.declared == "failed":
            return "failed", f"failed its test on {_d(self.validated_on)}: {self.evidence}"
        if self.declared == "validated":
            if self.validated_on is None or self.valid_until is None:
                return (
                    "unvalidated",
                    "validation dates missing, so the evidence cannot be shown to be current",
                )
            if today > self.valid_until:
                return "unvalidated", f"validation expired on {_d(self.valid_until)}: re-validate it"
            return "validated", f"validated on {_d(self.validated_on)}, valid until {_d(self.valid_until)}"
        return "unvalidated", self.evidence or "no validation yet"

    def to_json(self, today: date) -> dict:
        st, why = self.status(today)
        return {"key": self.key, "name": self.name, "kind": self.kind, "tier": self.tier,
                "required_validation": TIER_DEPTH[self.tier], "purpose": self.purpose,
                "intended_use": self.intended_use, "restrictions": self.restrictions,
                "limits_of_use": LIMITS_OF_USE, "inputs": list(self.inputs), "code": list(self.code),
                "last_update": self.last_update.isoformat(), "outcome_metric": self.outcome_metric,
                "status": st, "status_reason": why,
                "validated_on": self.validated_on.isoformat() if self.validated_on else None,
                "valid_until": self.valid_until.isoformat() if self.valid_until else None,
                "evidence": self.evidence, "regime_tags": list(self.regime_tags), "methods": list(self.methods),
                "red_team": list(self.red_team)}  # fmt: skip


def _d(d: date | None) -> str:
    return "an unknown date" if d is None else f"{d:%d-%b-%Y}"


def entries() -> list[ModelEntry]:
    from finresearch.inventory.entries import ENTRIES

    return list(ENTRIES)


def entry(key: str) -> ModelEntry | None:
    return next((e for e in entries() if e.key == key), None)


def entry_for_method(method: str) -> ModelEntry | None:
    """The entry whose method (exact or literal prefix) is the longest match for `method`."""
    hits = [(len(m), e) for e in entries() for m in e.methods if method == m or method.startswith(m)]
    return max(hits, key=lambda h: h[0])[1] if hits else None

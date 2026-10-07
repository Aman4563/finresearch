"""Point-in-time invariant for experiments (#244): every feature must be available at or before the decision it
informs (`available_at <= decision_at`).

An experiment states, per feature, WHEN the value was first knowable (a filing's broadcast time, a market close, the
publication of a final order book) and when the decision is taken (e.g. the 17:00 IST UPI mandate cut-off on an IPO's
closing day). `check` lists the features that arrive later. A known, accepted gap must be DECLARED (`allow`): the
result then carries the label that the model "uses post-cutoff data, not usable at the decision point", and the
model may only run as a shadow test. An undeclared late feature raises `LookAheadError`, so a new leak cannot slip
into a backtest unnoticed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

POST_CUTOFF_LABEL = "uses post-cutoff data, not usable at the decision point"


class LookAheadError(ValueError):
    """A feature that is only available after the decision it is used for, and was not declared."""


@dataclass(frozen=True)
class Feature:
    name: str
    available_at: datetime  # when the value was first knowable (aware datetime)
    # how that time is known, e.g. "NSE broadcast time" or "final book, after the 17:00 close"
    basis: str = ""


@dataclass
class TimingReport:
    decision: str  # the decision point in words
    checked: int = 0  # observations checked
    # feature -> observations where it arrived after the decision
    late: dict[str, int] = field(default_factory=dict)

    @property
    def usable_at_decision(self) -> bool:
        return not self.late

    def to_dict(self) -> dict[str, Any]:
        return {"decision_at": self.decision, "rows_checked": self.checked,
                "post_decision_features": sorted(self.late), "late_counts": dict(sorted(self.late.items())),
                "usable_at_decision": self.usable_at_decision,
                "label": None if self.usable_at_decision else POST_CUTOFF_LABEL}  # fmt: skip


def late(features: Iterable[Feature], decision_at: datetime) -> list[Feature]:
    """The features that were not yet available at `decision_at`."""
    if decision_at.tzinfo is None:
        raise ValueError("decision_at must be timezone-aware")
    out = []
    for f in features:
        if f.available_at.tzinfo is None:
            raise ValueError(f"{f.name}: available_at must be timezone-aware")
        if f.available_at > decision_at:
            out.append(f)
    return out


def check(features: Iterable[Feature], decision_at: datetime, *, allow: Iterable[str] = (),
          report: TimingReport | None = None, context: str = "") -> list[Feature]:  # fmt: skip
    """Assert `available_at <= decision_at` for every feature except the declared ones in `allow`, which are
    recorded on `report` instead. Raises LookAheadError naming any undeclared late feature."""
    bad = late(features, decision_at)
    allowed = set(allow)
    undeclared = [f for f in bad if f.name not in allowed]
    if undeclared:
        names = ", ".join(
            f"{f.name} (available {f.available_at:%Y-%m-%d %H:%M %Z}; {f.basis})" for f in undeclared
        )
        raise LookAheadError(
            f"{context or 'observation'}: decided at {decision_at:%Y-%m-%d %H:%M %Z} but used {names}"
        )
    if report is not None:
        report.checked += 1
        for f in bad:
            report.late[f.name] = report.late.get(f.name, 0) + 1
    return bad

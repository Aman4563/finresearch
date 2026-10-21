"""The owner's dated data-rights decision per collected dataset (#362, P1.0): a backfill or nightly job asks
`require(dataset)` before its first request and refuses to run without a recorded decision.

NSE's terms (Wayback memento of 01-Oct-2026, 'Updated on: 29/10/2025') prohibit "any systematic or automated data
collection activities" and use "for any gaming, virtual trading or simulation activities"; there is no personal-use
carve-out. The owner's decision of 21-Oct-2026 (issue #362) is to accept that risk for personal, non-distributed use
only, with these safeguards: the data stays in the local database and the gitignored data/ folder (never committed,
published or shared); polite access (the shared per-host rate limit, one archive file per dataset per day, backfills
spread out and run outside market hours); stop a dataset on repeated 403/429 or any notice and revisit the decision;
re-check NSE's terms before each new dataset.
"""

from __future__ import annotations

from dataclasses import dataclass


class NoDecision(RuntimeError):
    pass


@dataclass(frozen=True)
class Decision:
    dataset: str
    choice: str  # accept-personal | license | drop
    decided: str  # ISO date
    ref: str  # where the decision is recorded
    terms_read: str  # the terms version the decision rests on


_NSE_TERMS = "NSE website terms, Wayback memento of 01-Oct-2026 ('Updated on: 29/10/2025')"
DECISIONS: dict[str, Decision] = {
    d.dataset: d
    for d in (
        Decision(
            "index_eod", "accept-personal", "2026-10-21", "issue #362", _NSE_TERMS
        ),  # ind_close_all (#366)
        Decision(
            "nse_span", "accept-personal", "2026-10-21", "issue #362", _NSE_TERMS
        ),  # nsccl SPAN + FOVOLT (#370)
    )
}


def require(dataset: str) -> Decision:
    """The dataset's decision; NoDecision when none is recorded or the owner chose to drop it."""
    d = DECISIONS.get(dataset)
    if d is None:
        raise NoDecision(f"no recorded data-rights decision for {dataset!r} (#362): not collecting it")
    if d.choice == "drop":
        raise NoDecision(f"the owner dropped {dataset!r} on {d.decided} ({d.ref})")
    return d

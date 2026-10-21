"""The result of one data-quality check (#368)."""

from __future__ import annotations

from dataclasses import dataclass

STATUSES = ("ok", "warn", "fail", "unknown")


@dataclass(slots=True)
class Check:
    """status ok / warn / fail / unknown (could not be evaluated: never read as ok). A failed `critical` check marks
    the pages that read the dataset 'data unverified'; a non-critical one (a corporate-action jump, a re-upload) is
    shown in the brief but does not."""

    dataset: str
    check: str
    status: str
    value: float | None = None
    threshold: float | None = None
    note: str | None = None
    critical: bool = True

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown check status {self.status!r}")
        if self.note is not None:
            self.note = self.note[:300]


def share_status(bad: int, n: int, limit: float) -> tuple[str, float | None]:
    """fail when bad / n > limit; unknown with nothing to test."""
    if n == 0:
        return "unknown", None
    share = bad / n
    return ("fail" if share > limit else "ok"), share

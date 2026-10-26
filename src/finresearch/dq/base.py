"""The result of one data-quality check (#368)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

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


def soft(
    s: Session, dataset: str, names: Sequence[str], fn: Callable[..., list[Check]], *args: Any
) -> list[Check]:
    """A group of non-critical checks inside another dataset's pass (#368: the option screens, the control charts),
    run in its own savepoint: a crash, even a failed SQL statement, becomes non-critical unknown rows naming the
    exception's type only, so it never aborts the session or leaves the dataset critically unknown."""
    try:
        with s.begin_nested():
            return fn(s, *args)
    except Exception as e:
        log.warning("%s: non-critical checks %s failed", dataset, ", ".join(names), exc_info=True)
        return [
            Check(dataset, n, "unknown", note=f"screen failed: {type(e).__name__}", critical=False)
            for n in names
        ]

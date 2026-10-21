"""The gate pages read (#368): are the datasets a displayed number depends on verified?

verified: every dataset's latest check run passed (ok or a non-critical warn); unverified: a critical check failed
(the number is shown marked 'data unverified', never as ok); unknown: a dataset was never checked or a critical check
could not be evaluated. Unknown is never verified.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import DatasetStatus

# which pages read which datasets (the brief's Data health card lists them)
DEPENDENTS: dict[str, tuple[str, ...]] = {
    "F&O direction model": ("fno_bhav", "fno_iv_eod", "index_eod"),
    "F&O IV history and backtests": ("fno_bhav", "fno_iv_eod"),
    "Participant positioning": ("fno_participant_oi",),
    "Margin estimate (SPAN)": ("span",),
}


def health(s: Session, datasets: Iterable[str]) -> dict[str, Any]:
    names = tuple(dict.fromkeys(datasets))
    rows = {r.dataset: r for r in s.scalars(select(DatasetStatus).where(DatasetStatus.dataset.in_(names)))}
    out: dict[str, Any] = {}
    for n in names:
        r = rows.get(n)
        out[n] = ({"status": "unknown", "day": None, "failed": [], "checked_at": None} if r is None else
                  {"status": r.status, "day": r.day.isoformat(), "failed": list(r.failed or []),
                   "checked_at": r.checked_at.isoformat() if r.checked_at else None})  # fmt: skip
    failed = [n for n in names if out[n]["status"] == "fail"]
    status = "unverified" if failed else ("unknown" if any(v["status"] == "unknown" for v in out.values())
                                          else "verified")  # fmt: skip
    return {"status": status, "failed": failed, "datasets": out}

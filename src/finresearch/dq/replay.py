"""Historical replay of the content checks (#368 acceptance 2): every loaded bhavcopy day in [start, end] through the
bhavcopy, participant OI, derived IV and index close checks, read only (nothing is stored). Freshness is not replayed
(it is about the clock, not the data). The owner runs it on the real history with `finresearch dq replay`; acceptance:
every known incident flagged and at most 1 false alarm per 20 sessions on 2024-26."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import FnoBhavDay
from finresearch.dq import fno, ref

CHECKS = (fno.bhav_checks, ref.participant_checks, ref.iv_checks, ref.index_checks)


def replay(s: Session, start: date, end: date) -> dict[str, Any]:
    days = s.scalars(select(FnoBhavDay.day).where(FnoBhavDay.status == "loaded", FnoBhavDay.day.between(start, end))
                     .order_by(FnoBhavDay.day)).all()  # fmt: skip
    by_check: dict[str, Counter[str]] = defaultdict(Counter)
    flagged: dict[str, list[str]] = defaultdict(list)
    critical: list[str] = []
    for d in days:
        hit = False
        for fn in CHECKS:
            for c in fn(s, d):
                key = f"{c.dataset}:{c.check}"
                by_check[key][c.status] += 1
                if c.status in ("fail", "warn"):
                    flagged[key].append(d.isoformat())
                    hit = hit or (c.critical and c.status == "fail")
        if hit:
            critical.append(d.isoformat())
    s.rollback()  # read only
    return {"start": start.isoformat(), "end": end.isoformat(), "sessions": len(days),
            "by_check": {k: dict(v) for k, v in sorted(by_check.items())}, "flagged_days": dict(flagged),
            "critical_days": critical,
            "critical_rate": None if not days else len(critical) / len(days)}  # fmt: skip

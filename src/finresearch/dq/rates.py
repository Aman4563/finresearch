"""Checks on FBIL's dated rate series (#366, refseries.rates), one dataset per series (`rate_<series>`):

* expected_tenors: the tenors every published day carries (gsec_par 0.25Y and 10Y, tbill 3M, mibor_term 1M; overnight
  MIBOR prints ON or a 3D/4D weekend tenor, so any row will do).
* row_ranges: every rate a fraction in (0, RATE_MAX): a percent stored unconverted, or a zero, fails.
* reupload (not critical): FBIL re-published the day with other content (dq.lineage, dataset fbil_<series>).

Freshness (dq.run): FBIL's public pages lagged about a week on 22-Oct-2026, so up to MAX_LAG sessions behind is ok.
It counts NSE sessions (dq.stats with the NSE holiday list); FBIL follows the RBI/money-market calendar, so a bank
holiday that is an NSE session can show one session of lag that is not real. The checks are critical for their own
dataset status (a never-loaded series is unknown, never ok) but no page depends on them yet (dq.gate.DEPENDENTS).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import RateCurve
from finresearch.dq.base import Check

EXPECTED = {"gsec_par": ("0.25Y", "10Y"), "tbill": ("3M",), "mibor_on": (), "mibor_term": ("1M",)}
RATE_MAX = 0.20  # a sanity band, not a market view: no FBIL series has printed 20 %
MAX_LAG = 7


def checks_for(series: str) -> Callable[[Session, date], list[Check]]:
    from finresearch.dq.ref import _reupload
    from finresearch.refseries.rates import lineage_name

    name = f"rate_{series}"

    def run(s: Session, day: date) -> list[Check]:
        rows = s.scalars(select(RateCurve).where(RateCurve.series == series, RateCurve.day == day)).all()
        if not rows:
            return [
                Check(name, n, "unknown", note=f"{day} is not loaded")
                for n in ("expected_tenors", "row_ranges")
            ]
        have = {r.tenor for r in rows}
        miss = [t for t in EXPECTED[series] if t not in have]
        bad = [r.tenor for r in rows if not 0 < float(r.rate) < RATE_MAX]
        rup = _reupload(s, lineage_name(series), day)
        return [Check(name, "expected_tenors", "fail" if miss else "ok", len(miss), 0,
                      f"missing: {', '.join(miss)}" if miss else None),
                Check(name, "row_ranges", "fail" if bad else "ok", len(bad), RATE_MAX,
                      f"out of range: {', '.join(bad[:6])}" if bad else None),
                Check(name, "reupload", rup.status, rup.value, rup.threshold, rup.note, critical=False)]  # fmt: skip

    return run

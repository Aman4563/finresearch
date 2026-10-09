"""Fail-closed trading-day checks for the monitor's periodic market-data jobs (#247).

NSE publishes only the current year's holiday list (adapters.nse_holidays). When that year is not cached, a job that
reads the exchange after the close (stock peers, disclosures, IV history) cannot tell a holiday from a session, so
it does NOT run: running on a holiday wastes hundreds of NSE requests and can store the last session's figures under
a holiday's date. The day is logged and one warning alert per IST day says why the jobs are paused; they resume as
soon as the list is refreshed (monitor.scheduler._refresh_holidays retries hourly while the year is missing).

Not covered here (needs a decision, see the PR): the IPO watch plan (monitor.plan) and the intraday archive fall back
to "no holidays", because skipping a bidding-day check loses data that cannot be fetched later. Broker syncs
(portfolio.connectors.sync._holidays) do the same for that reason: Groww's order list answers today only (#266).
"""

from __future__ import annotations

import logging
from datetime import date

from finresearch.fincalc.dates import is_business_day

log = logging.getLogger(__name__)

ALERT_KIND = "holidays_unknown"
_ALERTED: set[date] = set()
_LOGGED: set[tuple[date, str]] = set()


def market_day(day: date, holidays: set[date] | None = None, *, job: str) -> bool:
    """True when `day` (IST) is a trading day. `holidays` given: those (tests and callers that already hold the
    list). None: the cached NSE list, and False when the year's list is unknown (logged and alerted once a day)."""
    if holidays is None:
        from finresearch.adapters.nse_holidays import known_trading_holidays

        holidays = known_trading_holidays(day)
        if holidays is None:
            if day.weekday() < 5:
                unknown_holidays(day, job)
            return False
    return is_business_day(day, holidays)


def unknown_holidays(day: date, job: str) -> None:
    """Log every skipped job; raise one warning alert per IST day (claimed in alert_eval_slot, so two monitor
    processes raise it once). An alert failure never breaks the tick."""
    if (day, job) not in _LOGGED:  # once per job and day: the monitor asks every minute
        _LOGGED.add((day, job))
        log.warning("NSE %s holiday list unknown: %s skipped for %s (fails closed)", day.year, job, day)
    if day in _ALERTED:
        return
    try:
        from sqlalchemy.dialects.postgresql import insert

        from finresearch.db import session_scope
        from finresearch.db.models import Alert, AlertEvalSlot

        with session_scope() as s:
            got = s.execute(insert(AlertEvalSlot).values(slot=f"{ALERT_KIND}:{day.isoformat()}",
                                                         result={"status": "done", "job": job})
                            .on_conflict_do_nothing(index_elements=["slot"]).returning(AlertEvalSlot.slot)).first()  # fmt: skip
            if got is not None:
                s.add(Alert(kind=ALERT_KIND, level="warn",
                            message=f"NSE's {day.year} holiday list is not loaded, so the after-close market jobs "
                                    "(stock peers, disclosures, IV history) are paused until it is refreshed.",
                            data={"day": day.isoformat(), "job": job}))  # fmt: skip
        _ALERTED.add(day)
    except Exception:
        log.warning("could not raise the holiday-list alert", exc_info=True)

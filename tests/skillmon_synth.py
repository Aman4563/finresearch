"""Synthetic VaR-ledger records for the skill monitor's risk-trigger tests (#378): made-up 1-day VaR outcomes on the
weekdays of a quarter, as `risk.Obs` or as forecast-ledger rows (fractions and dates only, as the ledger logs them).
No portfolio, no real value."""

from __future__ import annotations

from datetime import date, timedelta

from finresearch.skillmon import risk

Q1, Q2, Q3, Q4 = date(2026, 1, 1), date(2026, 4, 1), date(2026, 7, 1), date(2026, 10, 1)


def quarter(start: date, n: int, exceptions: int, *, first_id: int = 0, open_rows: int = 0, void: int = 0):
    """n logged days on the quarter's first weekdays: `exceptions` of them exceptions, then `open_rows` still open and
    `void` voided (all among the n)."""
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    out = []
    for i, day in enumerate(days):
        status = "open" if i >= n - open_rows else "void" if i >= n - open_rows - void else "resolved"
        outcome = None if status != "resolved" else int(i < exceptions)
        out.append(risk.Obs(day, status, outcome, first_id + i))
    return out


def store(obs: list, *, drop_as_of: int = 0) -> None:
    """Made-up VaR-ledger rows (fractions and dates only, as the ledger logs them); the first `drop_as_of` lose their
    day, as a malformed row would."""
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.portfolio import var_ledger as vl

    with session_scope() as s:
        for i, o in enumerate(obs):
            inputs = {"var": 0.02} if i < drop_as_of else {"as_of": o.day.isoformat(), "var": 0.02}
            s.add(Forecast(asset=vl.ASSET, instrument=vl.INSTRUMENT, source="monitor:portfolio", event_kind=vl.EVENT_KIND,
                           event=vl.EVENT, horizon="1 trading day", resolve_on=o.day + timedelta(days=1),
                           probability=0.05, action="RISK", method=vl.METHOD, validation_status="rule_based",
                           inputs=inputs, status=o.status, outcome=o.outcome, dedupe_key=f"var{i}"))  # fmt: skip

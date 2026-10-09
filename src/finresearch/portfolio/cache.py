"""What the monitor's daily portfolio pass leaves behind for the alert metrics, the brief and the dashboard strip.

Stored as JSON rows of `portfolio_setting` (no schema change) under keys starting with "cache:". They are derived
data: rebuilt by the next pass, left out of the data export, and personal (never sent to an LLM).

- cache:valuation  the latest valuation (per holding: price and its date, units, value, weight, sector) plus up to
                   HISTORY_DAYS earlier valuation days (price, units) for day moves and the weekly digest;
- cache:signals    each holding's signal (action, score, validation) today and the changes since the last pass;
- cache:events     corporate actions, board meetings and the latest results filing of stock holdings (NSE);
- cache:ter        each held fund's TER from AMFI's file, with the previous value when it changed;
- cache:valuation_gaps  what the latest recorded snapshot left out (portfolio.metrics.record_snapshot): the holdings
                   without a price (name, units, last known price from the valuation history), and how many were
                   valued at a stale price or have an unknown cost. Read by the goal plan's lower bound (#286).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioSetting

PREFIX = "cache:"
VALUATION, SIGNALS, EVENTS, TER = "cache:valuation", "cache:signals", "cache:events", "cache:ter"
GAPS = "cache:valuation_gaps"
HISTORY_DAYS = 10


def read(s: Session, key: str) -> dict[str, Any]:
    row = s.get(PortfolioSetting, key)
    return dict(row.value or {}) if row else {}


def write(s: Session, key: str, value: dict[str, Any]) -> None:
    row = s.get(PortfolioSetting, key)
    if row is None:
        s.add(PortfolioSetting(key=key, value=value))
    else:
        row.value = value
    s.flush()

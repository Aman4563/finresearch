"""Owner-entered risk-free yields (#479): the first tier of signals.rates, kept in `owner_rate`.

The owner types a yield as RBI quotes it (simple, Actual/365, in percent, e.g. RBI's 91-day cut-off of 5.7281 % on
14-Oct-2026), with its day and tenor ("91D"). Nothing is fetched. Written only by the POST API (api/rates.py) and
`finresearch rates enter`; read by signals.rates, which uses the latest entry per tenor and shifts it by any repo
change since its day (fincalc.riskfree).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import OwnerRate as Row
from finresearch.fincalc import policy_rates as P
from finresearch.fincalc import riskfree as R

TENOR_RE = re.compile(r"^(\d{1,3})D$")
MAX_PCT = Decimal(20)  # a sanity band for a typed percent, not a market view


def tenor_days(tenor: str) -> int:
    """'91D' -> 91; ValueError unless it is 1..364 days ('3M' is refused: say the days, as RBI's bills do)."""
    m = TENOR_RE.match(str(tenor).strip().upper())
    n = int(m.group(1)) if m else 0
    if not 1 <= n <= R.MAX_TENOR_DAYS:
        raise ValueError(f"tenor must be 1D to {R.MAX_TENOR_DAYS}D (days, e.g. 91D), not {tenor!r}")
    return n


def validate(day: date, tenor: str, pct: Decimal, today: date) -> int:
    if day < P.START:
        raise ValueError(f"{day} is before {P.START:%d-%b-%Y}, where the RBI policy-rate table starts")
    if day > today:
        raise ValueError(f"{day} is in the future")
    n = tenor_days(tenor)
    if not Decimal(0) < pct < MAX_PCT:
        raise ValueError(f"yield must be a percent between 0 and {MAX_PCT} (e.g. 5.7281), not {pct}")
    return n


def add(s: Session, day: date, tenor: str, pct: Decimal, source: str | None, *, today: date) -> R.OwnerRate:
    """Store (or replace) the owner's yield for `day` and `tenor`; `pct` in percent as RBI prints it."""
    n = validate(day, tenor, Decimal(pct), today)
    y = Decimal(pct) / 100
    src = (source or "").strip()[:300] or None
    stmt = insert(Row).values(day=day, tenor_days=n, simple_yield=y, source=src)
    s.execute(stmt.on_conflict_do_update(index_elements=["day", "tenor_days"],
                                         set_={"simple_yield": y, "source": src, "entered_at": stmt.excluded.entered_at}))  # fmt: skip
    return R.OwnerRate(day, n, y, src)


def remove(s: Session, day: date, tenor: str) -> bool:
    n = tenor_days(tenor)
    return bool(s.execute(delete(Row).where(Row.day == day, Row.tenor_days == n)).rowcount)


def entries(s: Session) -> list[R.OwnerRate]:
    """Every entry, oldest first."""
    rows = s.scalars(select(Row).order_by(Row.day, Row.tenor_days)).all()
    return [R.OwnerRate(r.day, r.tenor_days, Decimal(r.simple_yield), r.source, r.entered_at) for r in rows]

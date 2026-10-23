"""/api/rates/*: the risk-free rate (#479). Nothing here fetches a rate from anyone.

* GET /api/rates/risk-free?day=&tenor=91D: the answer of signals.rates for a day (default today, IST) and tenor:
  the owner-entered tier, else the RBI repo-rate proxy, else unknown, with tier, date and source.
* GET /api/rates/policy: fincalc.policy_rates' hand-typed table of RBI policy rates, each row with its RBI release.
* GET /api/rates/owner: the owner's entries. POST /api/rates/owner stores (or replaces) one; DELETE
  /api/rates/owner/{day}/{tenor} removes one. The GETs write nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class OwnerEntry(BaseModel):
    day: date
    tenor: str = Field(pattern=r"^\d{1,3}[Dd]$", description="term in days, e.g. 91D")
    yield_pct: Decimal = Field(
        gt=0, lt=20, description="the yield as RBI quotes it: % a year, simple, Actual/365"
    )
    source: str | None = Field(
        None, max_length=300, description="where you read it (e.g. RBI's auction result)"
    )


def _entry_json(e: Any) -> dict[str, Any]:
    return {"day": e.day.isoformat(), "tenor": f"{e.tenor_days}D", "yield_pct": float(e.yld * 100),
            "source": e.source}  # fmt: skip


def add_rates_routes(app: FastAPI, *, clock=None) -> None:
    now = clock or (lambda: datetime.now(UTC))

    def today() -> date:
        from finresearch.fincalc.dates import to_ist

        return to_ist(now()).date()

    @app.get("/api/rates/risk-free")
    def risk_free(day: date | None = None, tenor: str = "91D") -> dict[str, Any]:
        """The risk-free rate for `day` (default today) and `tenor` (days, e.g. 91D), naming its tier and source."""
        from finresearch.db import session_scope
        from finresearch.refseries.owner_rates import tenor_days
        from finresearch.signals import rates

        d, n = day or today(), tenor_days(tenor)
        with session_scope() as s:
            out = rates.rate_on(s, d, n / 365)
            s.rollback()  # read only
        return {"day": d.isoformat(), **out}

    @app.get("/api/rates/policy")
    def policy() -> dict[str, Any]:
        from finresearch.fincalc import policy_rates as P

        return {"start": P.START.isoformat(), "reviewed": P.REVIEWED.isoformat(),
                "rows": [{"effective": r.effective.isoformat(), "repo": float(r.repo), "floor": float(r.floor),
                          "floor_kind": r.floor_kind, "msf": float(r.msf), "title": r.title,
                          "released": r.released.isoformat(), "url": r.url} for r in P.TABLE]}  # fmt: skip

    @app.get("/api/rates/owner")
    def owner_list() -> dict[str, Any]:
        from finresearch.db import session_scope
        from finresearch.refseries import owner_rates

        with session_scope() as s:
            rows = owner_rates.entries(s)
            s.rollback()
        return {"entries": [_entry_json(e) for e in rows]}

    @app.post("/api/rates/owner", status_code=201)
    def owner_add(body: OwnerEntry) -> dict[str, Any]:
        """Store the owner's yield for a day and tenor (a new one replaces the old)."""
        from finresearch.db import session_scope
        from finresearch.refseries import owner_rates

        with session_scope() as s:
            e = owner_rates.add(s, body.day, body.tenor, body.yield_pct, body.source, today=today())
        return {"entry": _entry_json(e)}

    @app.delete("/api/rates/owner/{day}/{tenor}")
    def owner_remove(day: date, tenor: str) -> dict[str, Any]:
        from finresearch.db import session_scope
        from finresearch.refseries import owner_rates

        with session_scope() as s:
            if not owner_rates.remove(s, day, tenor):
                raise HTTPException(404, f"no owner-entered rate for {day} {tenor}")
        return {"removed": {"day": day.isoformat(), "tenor": tenor.upper()}}

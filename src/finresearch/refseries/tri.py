"""Total-return index (#366). NSE's official TRI is published on niftyindices.com (NSE Indices Ltd); it is not
fetched yet (its source and terms are an open decision on #366). Until then this is the APPROXIMATE series from the
stored index closes and dividend yields (refseries.index_eod, fincalc.refseries.approx_tri), labelled so in every
answer: TR_t = TR_t-1 x (P_t / P_t-1 + y_t / 252), based at the first close. A day with an unknown close or yield
stops the chain (that day and later are None), never a zero yield."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import IndexEod
from finresearch.fincalc import refseries as rs

METHOD = ("APPROXIMATE total-return index from NSE's daily close and dividend yield: TR_t = TR_t-1 x "
          "(P_t / P_t-1 + y_t / 252); NSE's official TRI reinvests actual dividends on their ex-dates")  # fmt: skip


def tri_series(s: Session, symbol: str, start: date, end: date) -> dict[str, Any]:
    """{"symbol", "official": False, "method", "rows": [(iso day, TRI or None)], "note"} for the stored days."""
    got = s.execute(select(IndexEod.day, IndexEod.close, IndexEod.div_yield)
                    .where(IndexEod.symbol == symbol, IndexEod.day.between(start, end))
                    .order_by(IndexEod.day)).all()  # fmt: skip
    chain = rs.approx_tri(
        [(d, None if c is None else float(c), None if y is None else float(y)) for d, c, y in got]
    )
    broken = next((d for d, v in chain if v is None), None)
    note = (
        None
        if broken is None
        else f"unknown close or dividend yield on {broken.isoformat()}: no TRI from there"
    )
    return {"symbol": symbol, "official": False, "method": METHOD,
            "rows": [(d.isoformat(), v) for d, v in chain], "note": note}  # fmt: skip

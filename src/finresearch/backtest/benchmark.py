"""The opportunity-cost line of the F&O backtest (#381): every strategy's out-of-sample net P&L after the owner's tax,
beside holding NIFTY on the same capital over the same days (inference.opportunity_cost).

The index leg is NIFTY's APPROXIMATE total-return index built from the stored daily index closes and dividend yield
(refseries.index_eod, fincalc.refseries.approx_tri): NSE's own TRI reinvests actual dividends on their ex-dates.
Computed when the report is read, from the app database, so a saved run carries no personal figure (the slab rate is
the owner's profile). Unknown index rows or slab -> the line says unknown, never 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.backtest import inference
from finresearch.fincalc.refseries import approx_tri

SYMBOL = "NIFTY"
BENCHMARK_LABEL = "NIFTY 50, approximate total return (closes + dividend yield), same capital, same days"
MAX_EDGE_GAP_DAYS = 7  # the first / last stored session must be this close to the window's ends


def index_window(rows: Sequence[tuple[date, float | None, float | None]], lo: date, hi: date
                 ) -> tuple[tuple[date, float], tuple[date, float]] | None:  # fmt: skip
    """((first day, TRI), (last day, TRI)) of the approximate TRI over [lo, hi]; None when the rows do not reach
    within MAX_EDGE_GAP_DAYS of both ends or the chain breaks on an unknown close or yield."""
    inside = sorted(r for r in rows if lo <= r[0] <= hi)
    if (
        len(inside) < 2
        or (inside[0][0] - lo).days > MAX_EDGE_GAP_DAYS
        or (hi - inside[-1][0]).days > MAX_EDGE_GAP_DAYS
    ):
        return None
    tri = approx_tri(inside)
    if tri[-1][1] is None or tri[0][1] is None:
        return None
    return (tri[0][0], tri[0][1]), (tri[-1][0], tri[-1][1])


def _rows(s: Session, lo: date, hi: date) -> list[tuple[date, float | None, float | None]]:
    from finresearch.db.models import IndexEod

    q = (select(IndexEod.day, IndexEod.close, IndexEod.div_yield)
         .where(IndexEod.symbol == SYMBOL, IndexEod.day >= lo, IndexEod.day <= hi).order_by(IndexEod.day))  # fmt: skip
    return [(d, None if c is None else float(c), None if y is None else float(y)) for d, c, y in s.execute(q)]


def owner_slab(s: Session) -> Decimal | None:
    """The owner's tax slab (profile tax_slab_pct) as a fraction; None when the profile cannot be read."""
    from finresearch.suggest.advisor import load_profile

    try:
        return Decimal(str(load_profile(s).tax_slab_pct)) / 100
    except Exception:  # an unreadable profile is an unknown slab, never a default
        return None


def attach(rep: dict[str, Any], s: Session, slab_rate: Decimal | None = None) -> dict[str, Any]:
    """Add `opportunity` to every strategy that has an out-of-sample window (in place; returns `rep`)."""
    slab = owner_slab(s) if slab_rate is None else slab_rate
    capital = float((rep.get("book") or {}).get("capital") or 0) or None
    cache: dict[tuple[str, str], Any] = {}
    for r in rep.get("strategies") or []:
        w = (r.get("windows") or {}).get("oos")
        if not w or w.get("total_net") is None:
            continue
        key = (w["from"], w["to"])
        if key not in cache:
            lo, hi = date.fromisoformat(w["from"]), date.fromisoformat(w["to"])
            cache[key] = index_window(_rows(s, lo, hi), lo, hi)
        win = cache[key]
        o = inference.opportunity_cost(w["total_net"], capital or 0.0, win[0] if win else None,
                                       win[1] if win else None, slab, trades=w.get("trades") or 0)  # fmt: skip
        if capital is None:
            o.update(index_after_tax=None, excess_after_tax=None, reason="book capital unknown")
        r["opportunity"] = {**o, "benchmark": BENCHMARK_LABEL, "window": list(key)}
    return rep

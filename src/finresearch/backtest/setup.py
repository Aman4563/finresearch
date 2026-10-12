"""Today's setup of a pre-registered spec (#307): the legs its rule picks at the last stored close, for a strategy
the backtest made a candidate. Analysis only: nothing is ordered, and the setup is shown with its evidence, never as
a guarantee."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from finresearch.backtest import engine
from finresearch.backtest.history import Clocked
from finresearch.backtest.specs import BOOK, IV_WARMUP_DAYS, Book, Spec


def current_setup(spec: Spec, history: Any, book: Book = BOOK) -> dict[str, Any]:
    """{"as_of", "status": entry | wait | no_data, "legs", "reason", ...} at the latest stored day."""
    sym = spec.underlying
    last = history.latest(sym)
    if last is None:
        return {"status": "no_data", "reason": f"no stored F&O history for {sym}"}
    h = Clocked(history)
    h.advance(last)
    x = h.day(sym, last)
    out: dict[str, Any] = {"as_of": last.isoformat(), "symbol": sym}
    if x is None:
        return {**out, "status": "no_data", "reason": f"no rows for {sym} on {last}"}
    ivs = h.iv_history(sym, last - timedelta(days=IV_WARMUP_DAYS), last)
    st = engine.SpecState(spec)
    p = engine.try_enter(st, x, sym, h, ivs, book)
    if p is None:
        later = [e for e in x.expiries() if (e - last).days > spec.dte_max]
        why = "; ".join(st.skips) or (
            f"no expiry {spec.dte_min}-{spec.dte_max} days away at this close"
            + (f"; the window opens on {later[0] - timedelta(days=spec.dte_max):%d-%b-%Y}" if later else "")
        )
        return {**out, "status": "wait", "reason": why}
    return {**out, "status": "entry", "expiry": p.expiry.isoformat(), "dte": p.dte, "lots": p.lots, "lot": p.lot,
            "legs": [{"right": lg.right, "strike": lg.strike, "side": lg.side, "ref_price": lg.mark,
                      "fill_assumed": lg.entry} for lg in p.legs],
            "premium": round(p.premium, 2), "max_loss": None if p.max_loss is None else round(p.max_loss, 2),
            "margin_approx": None if p.margin is None else round(p.margin, 2), "entry_costs": round(p.entry_costs, 2),
            "iv_percentile": p.iv_pct,
            "note": f"The rule's legs at the {last:%d-%b-%Y} close (end of day; prices have moved since). "
                    "Sized for the backtest's ₹10 lakh book: scale to your own F&O budget."}  # fmt: skip

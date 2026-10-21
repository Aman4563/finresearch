"""Race events from the card ledger (#376 -> #377): each resolved card (fnobook.card_forecasts) is one event, its
real-world and risk-neutral forecasts paired. This is the forward window's event source (PREREG: primary).

- Outcome and Rs P&L per set: the card ledger's resolution (the card's exit plan replayed on the bhavcopy); a void or
  open card is not an event.
- p_rw: the card's real-world PoP and EV; p_rn_atm: the risk-neutral PoP and EV. The card's 20 P&L dots belong to
  the model named in their label (the real-world scenario, else the risk-neutral one) and only to it; dots that are
  missing or not non-decreasing are unknown (None).
- Gate class: ENTER or PAPER (every check passed, no proven edge) passes; WAIT or AVOID fails; anything else unknown.
- `first`: the card ledger's independence rule (signals.ledger.independent_events): the first card logged per
  underlying, expiry and structure that carries a real-world probability and resolved.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import Forecast
from finresearch.fincalc import tradeplan as tp
from finresearch.fincalc.dates import to_ist
from finresearch.fnobook.card_forecasts import EVENT_KIND, RN_METHOD
from finresearch.horserace.race import Event
from finresearch.horserace.race import Forecast as Fc
from finresearch.signals import ledger

GATE_PASS, GATE_FAIL = (tp.ENTER, tp.PAPER), (tp.WAIT, tp.AVOID)


def _dots(inp: dict[str, Any], model: str) -> list[float] | None:
    d = inp.get("pnl_dots") or {}
    vals = d.get("values") or []
    if not str(d.get("model") or "").startswith(model) or not vals or any(v is None for v in vals):
        return None
    return list(map(float, vals)) if all(a <= b for a, b in pairwise(vals)) else None


def _gate(verdict: str | None) -> bool | None:
    return True if verdict in GATE_PASS else False if verdict in GATE_FAIL else None


def events_from_rows(rows: Sequence[Any]) -> list[Event]:
    """Pair each card's two ledger rows (real-world under PLAN_METHOD, risk-neutral under RN_METHOD) into an Event."""
    from finresearch.signals.tradeplan import PLAN_METHOD

    done = [
        r for r in rows if r.status == "resolved" and r.outcome is not None and r.resolution_value is not None
    ]
    cards: dict[tuple[str, int], dict[str, Any]] = {}
    for r in done:
        if r.method in (PLAN_METHOD, RN_METHOD):
            cards.setdefault((r.source, (r.inputs or {}).get("card_index", 0)), {})[r.method] = r
    firsts = {id(r) for r in ledger.independent_events(
        [c[PLAN_METHOD] for c in cards.values() if PLAN_METHOD in c and c[PLAN_METHOD].probability is not None])}  # fmt: skip
    out = []
    for c in cards.values():
        r = c.get(PLAN_METHOD) or c[RN_METHOD]
        inp, ev = r.inputs or {}, (r.inputs or {}).get("ev") or {}
        rw, rn = c.get(PLAN_METHOD), c.get(RN_METHOD)
        out.append(Event(
            underlying=inp["symbol"], expiry=date.fromisoformat(inp["expiry"]), structure=inp.get("structure") or "?",
            origin=date.fromisoformat(inp["entry_day"]), resolved_on=to_ist(r.resolved_at).date(),
            outcome=int(r.outcome), pnl=float(r.resolution_value), gate_pass=_gate(inp.get("verdict")),
            first=rw is not None and id(rw) in firsts,
            forecasts={"p_rw": Fc(rw.probability if rw else None, ev.get("real_world"), _dots(inp, "real-world")),
                       "p_rn_atm": Fc(rn.probability if rn else None, ev.get("risk_neutral"),
                                      _dots(inp, "risk-neutral"))},
        ))  # fmt: skip
    return sorted(out, key=lambda e: (e.origin, e.underlying, e.expiry, e.structure))


def load(s: Session) -> list[Event]:
    """Every resolved card forecast in the ledger as race events (read only)."""
    rows = s.scalars(
        select(Forecast).where(Forecast.event_kind == EVENT_KIND, Forecast.status == "resolved")
    ).all()
    return events_from_rows(list(rows))

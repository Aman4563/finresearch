"""The monthly post-trade review (#383, P2.8): per paper trade the implementation shortfall against the card it was
taken from, the daily P&L explain of every marked position (paper and Groww) by greeks and by full revaluation, plan
adherence of the closed paper trades and the exposure mix. The arithmetic is `fincalc.posttrade`; this module reads
the rows (paper_strategy, fno_exit_rule, card_log, fno_mark) and writes nothing (it serves a GET).

What stays unknown: a trade not taken from a card, or a card without live bid/ask mids, has no shortfall; a
position-day with a leg's price, quantity, spot or IV unknown (or legs that changed between the two marks) has no
explain. Real (Groww) trades have no fills here yet (Groww's order and trade lists are not read), so their shortfall
and adherence are unknown. Analysis only: nothing here places an order. Local only: never sent to a model."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from finresearch.db.models import CardLog, PaperStrategy
from finresearch.fincalc import posttrade as pt


def card_of(s: Session, st: PaperStrategy) -> tuple[dict[str, Any] | None, str | None]:
    """The card a paper trade was taken from, or (None, why)."""
    from finresearch.fnobook.card_forecasts import _cards

    if st.card_log_id is None:
        return None, "not taken from a card"
    row = s.get(CardLog, st.card_log_id)
    cards = _cards(row.kind, row.response or {}) if row is not None else []
    if not 0 <= (st.card_index or 0) < len(cards):
        return None, f"card log {st.card_log_id} has no card {st.card_index}"
    return cards[st.card_index or 0], None


def shortfall_of(s: Session, st: PaperStrategy) -> dict[str, Any]:
    """IS of the entry against the card's mids: each paper leg is matched to the card leg with its right, strike and
    side; the fees are the trade's entry costs."""
    card, why = card_of(s, st)
    out: dict[str, Any] = {"paper_id": st.id, "card_log_id": st.card_log_id, "price": None, "fees": None,
                           "total": None, "base": None, "pct": None, "why": why}  # fmt: skip
    if card is None:
        return out
    entry = card.get("entry") or {}
    mids = [x.get("mid") for x in entry.get("legs") or []] if entry.get("status") == "ok" else []
    by_leg = {(x.get("right"), float(x.get("strike") or 0), x.get("side")): (mids[i] if i < len(mids) else None)
              for i, x in enumerate(card.get("legs") or [])}  # fmt: skip
    legs = []
    for x in st.legs:
        key = (x.right, float(x.strike), x.side)
        if key not in by_leg:
            return {**out, "why": f"the card has no {x.side} {x.right} {float(x.strike):g} leg"}
        legs.append(pt.FillLeg(x.side, x.qty, float(x.entry_price), by_leg[key]))
    got = pt.shortfall(legs, float(st.entry_costs))
    r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {**out, "price": r2(got.price), "fees": r2(got.fees), "total": r2(got.total), "base": r2(got.base),
            "pct": None if got.pct is None else round(got.pct, 3),
            "why": "the card has no live bid/ask mid for a leg" if got.why and "mid" in got.why else got.why}  # fmt: skip

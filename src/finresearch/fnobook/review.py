"""The monthly post-trade review (#383, P2.8): per paper trade the implementation shortfall against the card it was
taken from, the daily P&L explain of every marked position (paper and Groww) by greeks and by full revaluation, plan
adherence of the closed paper trades and the exposure mix. The arithmetic is `fincalc.posttrade`; this module reads
the rows (paper_strategy, fno_exit_rule, card_log, fno_mark) and writes nothing (it serves a GET).

What stays unknown: a trade not taken from a card, or a card without live bid/ask mids, has no shortfall; a
position-day with a leg's price, quantity, spot or IV unknown (or legs that changed between the two marks) has no
explain. Real (Groww) trades have no fills here yet (Groww's order and trade lists are not read), so their shortfall
and adherence are unknown. Analysis only: nothing here places an order. Local only: never sent to a model."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import CardLog, FnoMark, PaperStrategy
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


# --------------------------------------------------------------------------- the daily P&L explain
RATE = 0.065  # the rate the marks' IVs were implied at (fnobook.marks.RATE)
LOOKBACK = timedelta(days=14)  # how far before the month a previous mark is looked for
TARGET_SHARE = 0.8  # the acceptance: within tolerance on >= 80 % of position-days


def _paper_max_loss(s: Session, paper_id: int, spot: float | None) -> float | None:
    from finresearch.fnobook import positions as pos

    st = s.get(PaperStrategy, paper_id)
    if st is None:
        return None
    legs = [{"direction_known": True, "right": x.right, "strike": float(x.strike), "avg_price": float(x.entry_price),
             "qty": x.qty if x.side == "buy" else -x.qty} for x in st.legs]  # fmt: skip
    return pos.max_loss(legs, spot)[0]


def _pair(
    s: Session, key: str, d0: date, d1: date, m0: dict[str, FnoMark], m1: dict[str, FnoMark]
) -> dict[str, Any]:
    out: dict[str, Any] = {"position": key, "day": d1.isoformat(), "previous": d0.isoformat(), "actual": None,
                           "greeks": None, "revaluation": None, "max_loss": None, "within": None, "why": None}  # fmt: skip
    if set(m0) != set(m1) or any(m0[k].qty != m1[k].qty for k in m0):
        return {**out, "why": "the legs changed between the two marks"}
    rows = [(m0[k], m1[k]) for k in sorted(m0)]
    if any(a.qty is None for a, _ in rows):
        return {**out, "why": "a leg's direction is unknown"}
    if any(a.price is None or b.price is None for a, b in rows):
        return {**out, "why": "a leg's price is unknown on one of the two days"}
    a0, a1 = rows[0]
    if a0.spot is None or a1.spot is None or a0.t_years is None or a1.t_years is None:
        return {**out, "why": "the underlying's close is unknown on one of the two days"}
    actual = sum(a.qty * float(b.price - a.price) for a, b in rows)  # type: ignore[operator,misc]
    f = lambda v: None if v is None else float(v)  # noqa: E731
    legs = [pt.MarkLeg(a.right, f(a.strike) or 0.0, a.qty, f(a.iv), f(b.iv)) for a, b in rows]  # type: ignore[arg-type]
    kw = dict(s0=float(a0.spot), s1=float(a1.spot), t0=float(a0.t_years), t1=float(a1.t_years), rate=RATE,
              actual=actual)  # fmt: skip
    g = pt.legs_greek_explain(legs, d_days=(d1 - d0).days, **kw)  # type: ignore[arg-type]
    r = pt.revaluation(legs, **kw)  # type: ignore[arg-type]
    ml = _paper_max_loss(s, a0.paper_id, float(a0.spot)) if a0.paper_id is not None else None
    r2 = lambda v: round(v, 2)  # noqa: E731
    return {**out, "actual": r2(actual), "max_loss": None if ml is None else r2(ml),
            "greeks": None if g is None else {"delta": r2(g.delta), "gamma": r2(g.gamma), "vega": r2(g.vega),
                                              "theta": r2(g.theta), "explained": r2(g.explained),
                                              "residual": r2(g.residual),
                                              "unexplained": None if g.unexplained is None else round(g.unexplained, 4)},
            "revaluation": None if r is None else {"spot": r2(r.spot), "vol": r2(r.vol), "time": r2(r.time),
                                                   "model": r2(r.model), "residual": r2(r.residual)},
            "within": pt.within_tolerance(residual=None if g is None else g.residual, actual=actual, max_loss=ml),
            "why": None if g is not None else "a leg's implied volatility is unknown"}  # fmt: skip


def explain_days(s: Session, start: date, end: date) -> list[dict[str, Any]]:
    """Every position-day ending in [start, end): consecutive marks of one position (a paper trade, or a Groww
    underlying + expiry group), in day order."""
    groups: dict[str, dict[date, dict[str, FnoMark]]] = defaultdict(lambda: defaultdict(dict))
    for m in s.scalars(select(FnoMark).where(FnoMark.day >= start - LOOKBACK, FnoMark.day < end)):
        key = f"paper:{m.paper_id}" if m.book == "paper" else f"real:{m.position_key}"
        groups[key][m.day][m.leg_key] = m
    out = []
    for key, by_day in groups.items():
        out += [
            _pair(s, key, d0, d1, by_day[d0], by_day[d1])
            for d0, d1 in pairwise(sorted(by_day))
            if d1 >= start
        ]
    return sorted(out, key=lambda x: (x["day"], x["position"]))


def explain_summary(days: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [d["within"] for d in days if d["within"] is not None]
    share = sum(decided) / len(decided) if decided else None
    return {"position_days": len(days), "unknown": sum(d["actual"] is None or d["greeks"] is None for d in days),
            "decided": len(decided), "within": sum(decided), "share_within": None if share is None else round(share, 4),
            "target_share": TARGET_SHARE, "meets_target": None if share is None else share >= TARGET_SHARE}  # fmt: skip

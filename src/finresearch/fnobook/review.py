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

from finresearch.db.models import CardLog, FnoExitRule, FnoMark, PaperStrategy
from finresearch.fincalc import posttrade as pt
from finresearch.fincalc.dates import to_ist


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


# --------------------------------------------------------------------------- the month
DEFERRED_AFTER = 100  # MF16: disposition-effect and loss-chasing metrics wait for >= 100 closed trades


def _month(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    return start, date(year + month // 12, month % 12 + 1, 1)


def _closed_trade(s: Session, st: PaperStrategy) -> pt.ClosedTrade:
    from finresearch.fnobook import book
    from finresearch.fnobook.card_forecasts import exit_plan

    rules = [pt.RuleState(r.kind, float(r.threshold), r.fired_at)
             for r in s.scalars(select(FnoExitRule).where(FnoExitRule.paper_id == st.id))]  # fmt: skip
    card, _ = card_of(s, st)
    plan = exit_plan(card)[0] if card is not None else None
    base = abs(book.pp.net_premium([x for x in book.legs_of(st) if x.right != "future"])) or None
    return pt.ClosedTrade(st.close_reason or "", st.closed_at, None if st.gross_pnl is None else float(st.gross_pnl),  # type: ignore[arg-type]
                          base, rules, None if plan is None else bool(plan["hold_to_expiry"]))  # fmt: skip


def month_view(s: Session, year: int, month: int) -> dict[str, Any]:
    """The month's review: shortfall of the paper trades opened in it, adherence of those closed in it, the exposure
    mix of those opened in it and the P&L explain of the position-days in it (IST days)."""
    start, end = _month(year, month)
    rows = list(s.scalars(select(PaperStrategy).order_by(PaperStrategy.opened_at, PaperStrategy.id)))
    day = lambda d: to_ist(d).date()  # noqa: E731
    opened = [r for r in rows if start <= day(r.opened_at) < end]
    closed = [r for r in rows if r.status == "closed" and r.closed_at and start <= day(r.closed_at) < end]
    ad = pt.adherence([_closed_trade(s, r) for r in closed])
    isf = [shortfall_of(s, r) for r in opened]
    known = [x for x in isf if x["total"] is not None]
    mean = lambda xs: None if not xs else round(sum(xs) / len(xs), 2)  # noqa: E731
    mix = pt.exposure_mix([pt.MixTrade(day(r.opened_at), r.expiry, [(x.side, float(x.entry_price), x.qty)
                                                                    for x in r.legs if x.right != "future"],
                                       (day(r.closed_at) - day(r.opened_at)).days if r.closed_at else None)
                           for r in opened])  # fmt: skip
    days = explain_days(s, start, end)
    r4 = lambda v: None if v is None else round(v, 4)  # noqa: E731
    n_closed = sum(r.status == "closed" for r in rows)
    return {
        "month": f"{year}-{month:02d}", "start": start.isoformat(), "end": end.isoformat(),
        "adherence": {"closed": len(closed), "counts": ad.counts, "rate": r4(ad.rate), "with_stop": ad.with_stop,
                      "beyond_stop_count": ad.beyond_stop_count, "mean_beyond_stop": mean([ad.mean_beyond_stop]
                      if ad.mean_beyond_stop is not None else []), "window_hours": pt.PLANNED_WINDOW_HOURS},
        "shortfall": {"trades": isf, "known": len(known), "mean_total": mean([x["total"] for x in known]),
                      "mean_pct": mean([x["pct"] for x in known if x["pct"] is not None])},
        "mix": {"trades": mix.trades, "premium_sold_share": r4(mix.premium_sold_share),
                "same_day_expiry_share": r4(mix.same_day_expiry_share), "mean_hold_share": r4(mix.mean_hold_share)},
        "explain": {"days": days, "summary": explain_summary(days)},
        "deferred": {"closed_trades": n_closed, "starts_at": DEFERRED_AFTER,
                     "status": "deferred" if n_closed < DEFERRED_AFTER else "due"},
        "notes": NOTES,
    }  # fmt: skip


NOTES = [
    "Paper trades only for shortfall and adherence: Groww's order and trade lists are not read yet, so real fills "
    "and real exits are unknown. Real positions appear in the P&L explain from their nightly marks.",
    "Shortfall = Σ side × (fill − the card's mid) × qty + entry costs (positive = a cost), against the card the trade "
    "was taken from; % of |the net premium at the card's mids|. Paper fills are the LTP moved by the slippage.",
    "Explain: dV ≈ Δ dS + ½ Γ dS² + vega dσ + Θ dt at the previous night's mark (Black-Scholes at the IV implied by "
    "NSE's settlement price); full revaluation reprices spot → vol → time. A day is within tolerance when |residual| ≤ "
    "max(10 % of |actual|, 0.5 % of the max loss); the target is ≥ 80 % of decided position-days.",
    f"Adherence: a square-off within {pt.PLANNED_WINDOW_HOURS} h of the first exit rule that fired is planned, later "
    "is late; with rules armed and none fired, a square-off is discretionary and an expiry is planned (time); a trade "
    "with no rules and no card plan has no plan and is left out of the rate.",
    "Disposition effect and loss-chasing wait for 100 closed trades (MF16). Descriptive only: no causal claim.",
]


def brief_line(s: Session, today: date) -> str | None:
    """One line for the daily brief about this month so far; None when the month has no paper trade and no marks."""
    v = month_view(s, today.year, today.month)
    ad, isf, ex = v["adherence"], v["shortfall"], v["explain"]["summary"]
    if not ad["closed"] and not isf["trades"] and not ex["position_days"]:
        return None
    judged = sum(n for k, n in ad["counts"].items() if k != "no_plan")
    adh = ("plan adherence unknown (no closed trade with a plan)" if ad["rate"] is None else
           f"plan adherence {ad['rate']:.0%} ({round(ad['rate'] * judged)} of {judged} with a plan)")  # fmt: skip
    sf = f"shortfall known for {isf['known']} of {len(isf['trades'])} entries"
    if isf["mean_total"] is not None:
        sf += f" (mean ₹{isf['mean_total']:,.0f})"
    exp = ("P&L explain: no position-days yet" if not ex["position_days"] else
           f"P&L explain within tolerance on {ex['within']} of {ex['decided']} decided position-days")  # fmt: skip
    return f"F&O review ({today:%b %Y}): {ad['closed']} paper trades closed, {adh}; {sf}; {exp}."

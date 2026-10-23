"""The joint risk view's loader (#397): the investment portfolio's holdings with their betas, the open F&O book
(paper and Groww) priced on today's chains, and `fincalc.joint_risk` over both. Analysis only: nothing here places,
modifies or cancels an order. Reads the value history, option chains, stored index closes and `fno_mark`; writes no
record (GET /api/portfolio/analytics/joint-risk). Local only: both books are personal data and never reach a model.

Holding betas are portfolio.analytics' 1-year betas to NIFTYBEES (the same estimator as the hypothetical fall and the
stress replay, `stress_replay.estimate_beta`, MIN_BETA days); the user's beta-1 option (#423) gives short-history
equity a labelled beta 1. The book is `fnobook.scenarios`' legs, marks, parity forwards and registered betas.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc import joint_risk as jr
from finresearch.fincalc.dates import to_ist
from finresearch.portfolio.analytics import MIN_BETA

BOOK_START = date(2026, 10, 12)  # the paper book's first day (#397); Groww keeps only current positions


def holdings(h: Any, *, assume_beta1: bool = False) -> list[jr.Holding]:
    """Each position with a value and its 1-year beta to NIFTYBEES, or None with the reason."""
    from finresearch.portfolio.stress_replay import estimate_beta

    b = h.benchmark
    out = []
    for p in h.positions:
        if p.value <= 0:
            continue
        got = None
        if b is not None and h.days:
            got = estimate_beta(h.closes.get(p.key) or {}, h.factors.get(p.key), b.closes, b.factors,
                                h.days[-1], min_days=MIN_BETA)  # fmt: skip
        if got is not None:
            out.append(jr.Holding(p.key, p.name, p.value, got[0], f"beta {got[0]:.2f} ({got[1]} days)"))
        elif b is None:
            out.append(jr.Holding(p.key, p.name, p.value, None, "NIFTYBEES history unavailable"))
        elif p.tax_class == "equity" and assume_beta1:
            out.append(
                jr.Holding(p.key, p.name, p.value, 1.0, "beta 1 assumed (your option; short price history)")
            )
        else:
            out.append(jr.Holding(p.key, p.name, p.value, None, f"under a year of prices, needs {MIN_BETA} days "
                                                                "for a beta"))  # fmt: skip
    return out


def replay_move(payload: dict[str, Any]) -> tuple[float | None, tuple[str, ...]]:
    """The holdings' ₹ move from a one-day stress_replay window: known only when every holding has a path."""
    if not payload.get("available"):
        return None, (f"the holdings' replay: {payload.get('reason') or 'unavailable'}",)
    gone = payload.get("unmapped") or []
    if gone:
        return None, tuple(f"{u['name']}: no price path that day ({u['reason']})" for u in gone)
    return float(payload["loss"]["inr"]), ()


async def replay_moves(h: Any, fetch: Any, store: Any, today: date, *,
                       index_series: Any = None) -> dict[date, tuple[float | None, tuple[str, ...]]]:  # fmt: skip
    """The holdings' own move on each of book_scenarios.REPLAYS' days (stress_replay, one-day windows)."""
    from finresearch.portfolio import stress_replay as sr

    out = {}
    for r in bs.REPLAYS:
        w = sr.Window(f"day{r.day:%Y%m%d}", r.label, r.day, r.day, r.source)
        try:
            out[r.day] = replay_move(await sr.window(w, h, fetch, store, today, index_series=index_series))
        except Exception as e:  # one day's failure is that day's reason
            out[r.day] = (None, (f"the holdings' replay could not be computed ({type(e).__name__})",))
    return out


async def book(now: datetime, *, sources: Any = None, rate: float | None = None,
               candidate: Sequence[bs.BookLeg] = ()) -> tuple[jr.Book, list[bs.BookLeg], list[str]]:  # fmt: skip
    """The open book (None legs = unknown, with the reason) and `candidate` legs, all priced on today's chains, at
    `rate`, else FBIL's par yield at the longest leg's tenor (fnobook.scenarios.live_rate, named in a "rate: " note)."""
    from finresearch.fnobook import scenarios as sc
    from finresearch.fnobook import service

    sources = sources or service._sources()
    paper = sc.paper_legs()
    real, why = await sc.groww_legs(to_ist(now).date())
    notes = [why] if why else []
    legs = [*paper, *(real or []), *candidate]
    if legs:
        rinfo = await sc.live_rate(legs, now, rate)
        notes.append(sc.rate_note(rinfo))
        priced, m = (await sc._priced(legs, now, sources, rinfo["rate"]))[:2]
    else:  # nothing open: nothing is priced, so no rate is read
        priced, m = [], bs.Market(now, rate or 0.0, {})
    n = len(priced) - len(candidate)
    b = jr.Book(priced[:n] if real is not None else None, m, None if real is not None else why)
    return b, priced[n:], notes


def stored_test(s: Session, today: date) -> dict[str, Any]:
    """#397 (b) on the stored daily joint P&L (joint_pnl_day, monitor.joint_pnl): the pre-registered gate on the first
    MIN_DAYS usable sessions since BOOK_START with |NIFTY move| > BIG_MOVE (evaluated once: later days do not re-test
    it), the holdings' single-beta R² on the same days, and every unusable session counted with its reasons."""
    from collections import Counter

    from finresearch.db.models import JointPnlDay

    rows = s.scalars(select(JointPnlDay).where(JointPnlDay.day.between(BOOK_START, today))
                     .order_by(JointPnlDay.day)).all()  # fmt: skip
    f = lambda x: None if x is None else float(x)  # noqa: E731
    big = [r for r in rows if r.usable and abs(float(r.nifty_move)) > jr.BIG_MOVE]
    bad = [r for r in rows if not r.usable]
    first = big[: jr.MIN_DAYS]
    ft = jr.forward_test([(float(r.nifty_move), float(r.actual), float(r.explained)) for r in first])
    hb = [(f(r.holdings_actual), f(r.holdings_beta)) for r in first]
    hr2 = (jr.variance_explained([a for a, _ in hb], [b for _, b in hb])  # type: ignore[misc]
           if ft.r2 is not None and all(a is not None and b is not None for a, b in hb) else None)  # fmt: skip
    reasons: Counter[str] = Counter(w for r in bad for w in r.reasons)
    return {"usable_qualifying": len(big), "evaluated_on": len(first) if ft.r2 is not None else 0,
            "r2": ft.r2, "passed": ft.passed, "holdings_beta_r2": hr2, "stored_days": len(rows),
            "unusable": len(bad), "move_unknown": sum(r.nifty_move is None for r in bad),
            "unusable_qualifying": sum(r.nifty_move is not None and abs(float(r.nifty_move)) > jr.BIG_MOVE
                                       for r in bad),
            "unusable_reasons": dict(reasons.most_common(10))}  # fmt: skip


def forward_status(s: Session, today: date) -> dict[str, Any]:
    """#397 (b): sessions since BOOK_START with marks (fno_mark) and |NIFTY close-to-close move| > BIG_MOVE, against
    the MIN_DAYS the pre-registered test needs (evals/experiments/joint_risk/PREREG.md), and the test itself on the
    stored daily joint P&L (`stored_test`)."""
    from finresearch.db.models import FnoMark, IndexEod

    marked = sorted(set(s.scalars(select(FnoMark.day).where(FnoMark.day.between(BOOK_START, today)))))
    closes = {d: float(c) for d, c in s.execute(select(IndexEod.day, IndexEod.close).where(
        IndexEod.symbol == "NIFTY", IndexEod.day.between(BOOK_START - timedelta(days=10), today),
        IndexEod.close.is_not(None)))}  # fmt: skip
    n = 0
    for d in marked:
        prev = [k for k in closes if d - timedelta(days=7) <= k < d]
        if d in closes and prev:
            n += abs(closes[d] / closes[max(prev)] - 1) > jr.BIG_MOVE
    st = stored_test(s, today)
    u, bad = st["usable_qualifying"], st["unusable"]
    tail = f"; {u} usable in the stored joint P&L, {bad} stored session(s) unusable (counted with reasons)"
    if st["r2"] is not None:
        status = (f"evaluated on the first {st['evaluated_on']} usable qualifying days: R² {st['r2']:.3f} against "
                  f"the {jr.R2_GATE:.2f} gate, {'passed' if st['passed'] else 'failed'}")  # fmt: skip
    elif n < jr.MIN_DAYS:
        status = (f"not evaluable yet: {n} of {jr.MIN_DAYS} qualifying days (sessions with marks and a NIFTY move "
                  f"beyond {jr.BIG_MOVE:.1%})" + tail)  # fmt: skip
    else:
        status = f"not evaluable yet: {n} qualifying days but only {u} of {jr.MIN_DAYS} usable" + tail
    return {"qualifying_days": n, "needed": jr.MIN_DAYS, "big_move": jr.BIG_MOVE, "r2_gate": jr.R2_GATE,
            "r2": st["r2"], "passed": st["passed"], "since": BOOK_START.isoformat(), "status": status,
            "stored": st}  # fmt: skip


# --------------------------------------------------------------------------- payloads
METHOD = ("D_total = Σ β_i V_i over holdings + Σ β_u Δ F q over F&O legs (₹ per unit NIFTY move; the table shows it "
          "per 1 %). L(m) = Σ β_i V_i m + the book's full revaluation at NIFTY m one session from now (spot-vol "
          "response, each underlying at the worse end of its beta band). Historical days use each holding's own "
          "move that day. Instructive, not a forecast; analysis only.")  # fmt: skip
HEDGE_NOTE = ("The costed hedge calculator (#397) is built only once F&O is at least 25 % of the L(-10 %) loss; it "
              "would be a scenario, never a default programme.")  # fmt: skip


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def delta_json(d: jr.Delta) -> dict[str, Any]:
    return {"holdings": _r(d.holdings), "fno": _r(d.fno), "total": _r(d.total),
            "per_pct": {k: _r(None if v is None else v / 100) for k, v in
                        (("holdings", d.holdings), ("fno", d.fno), ("total", d.total))},
            "complete": d.complete, "reasons": list(d.reasons)}  # fmt: skip


def loss_json(x: jr.Loss) -> dict[str, Any]:
    return {"move": x.move, "holdings": _r(x.holdings), "fno": _r(x.fno), "total": _r(x.total),
            "fno_share": _r(x.fno_share, 4), "share_reason": x.share_reason, "reasons": list(x.reasons)}  # fmt: skip


def view(hs: jr.Holdings, b: jr.Book, notes: Sequence[str], at: datetime,
         replay_holdings: dict[date, tuple[float | None, tuple[str, ...]]], forward: dict[str, Any],
         holidays: frozenset[date] | set[date] = frozenset()) -> dict[str, Any]:  # fmt: skip
    """Everything GET /api/portfolio/analytics/joint-risk shows (besides the route's base fields)."""
    ls = jr.losses(hs, b, at)
    ht = jr.hedge_trigger(ls)
    reps = jr.replays(replay_holdings, b, holidays)
    return {
        "method": METHOD, "beta_note": jr.BETA_NOTE, "notes": list(notes),
        "holdings": [] if isinstance(hs, str) else [{"key": h.key, "name": h.name, "value": _r(h.value),
                                                     "beta": _r(h.beta, 4), "how": h.how} for h in hs],
        "holdings_reason": hs if isinstance(hs, str) else None,
        "book": {"known": b.legs is not None, "legs": None if b.legs is None else len(b.legs), "reason": b.reason},
        "d_total": delta_json(jr.d_total(hs, b)),
        "losses": [loss_json(x) for x in ls],
        "hedge_trigger": {"share": _r(ht.share, 4), "fired": ht.fired, "threshold": ht.threshold,
                          "reasons": list(ht.reasons), "note": HEDGE_NOTE},
        "replays": [{"day": r.day.isoformat(), "label": r.label, "nifty_move": r.nifty_move,
                     "iv30_change": r.iv30_change, "holdings": _r(r.holdings), "fno": _r(r.fno), "total": _r(r.total),
                     "reasons": list(r.reasons), "notes": list(r.notes)} for r in reps],
        "forward_test": forward,
    }  # fmt: skip


def pretrade_json(p: jr.PreTrade) -> dict[str, Any]:
    return {"d_before": delta_json(p.before), "d_after": delta_json(p.after), "l_before": loss_json(p.l_before),
            "l_after": loss_json(p.l_after), "d_change": _r(p.d_change), "l_change": _r(p.l_change),
            "trade_delta": _r(p.trade_delta), "trade_l": _r(p.trade_l), "trade_reasons": list(p.trade_reasons),
            "move": p.l_before.move}  # fmt: skip

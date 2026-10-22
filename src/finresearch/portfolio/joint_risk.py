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
    """The open book (None legs = unknown, with the reason) and `candidate` legs, all priced on today's chains."""
    from finresearch.fnobook import scenarios as sc
    from finresearch.fnobook import service

    sources = sources or service._sources()
    rate = sc.RATE if rate is None else rate
    paper = sc.paper_legs()
    real, why = await sc.groww_legs(to_ist(now).date())
    notes = [why] if why else []
    legs = [*paper, *(real or []), *candidate]
    priced, m = (await sc._priced(legs, now, sources, rate))[:2] if legs else ([], bs.Market(now, rate, {}))
    n = len(priced) - len(candidate)
    b = jr.Book(priced[:n] if real is not None else None, m, None if real is not None else why)
    return b, priced[n:], notes


def forward_status(s: Session, today: date) -> dict[str, Any]:
    """#397 (b): sessions since BOOK_START with marks (fno_mark) and |NIFTY close-to-close move| > BIG_MOVE, against
    the MIN_DAYS the pre-registered test needs (evals/experiments/joint_risk/PREREG.md)."""
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
    return {"qualifying_days": n, "needed": jr.MIN_DAYS, "big_move": jr.BIG_MOVE, "r2_gate": jr.R2_GATE,
            "r2": None, "passed": None, "since": BOOK_START.isoformat(),
            "status": f"not evaluable yet: {n} of {jr.MIN_DAYS} qualifying days (sessions with marks and a NIFTY "
                      f"move beyond {jr.BIG_MOVE:.1%})" if n < jr.MIN_DAYS else
                      f"{n} qualifying days: the pre-registered test can run (not wired yet)"}  # fmt: skip

"""The F&O book's history blocks of GET /api/fno/scenarios (#394): the historical scan at h = 1 and 5 sessions with
its 99th-percentile loss, historical ES97.5 and 10 worst dates, the multi-week episodes, the gap and expiry-day
scenarios (fincalc.book_history), and the liquidity term. Analysis only: nothing here trades. Local only: the F&O book
never reaches a model. Reads stored series (fnohist.book_history); writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from finresearch.fincalc import book_history as bh
from finresearch.fincalc import book_scenarios as bs

SCAN_H = (1, 5)  # sessions (#394: h in {1, 5})
ES_H = 1  # the ES the book is held to (the risk alert's) is the one-session one
LIQ_REASON = ("Liquidity-adjusted stress needs P1.1's quantile model of the half-spread at the scenario's moneyness "
              "and DTE and the measured stress multiplier k (#363); neither is fitted yet, so it is unknown.")  # fmt: skip
LIQ_NOTE = ("Today's quoted exit cost: |q| x (ask - bid) / 2 summed over the legs, at today's quotes. Not a stressed "
            "figure: spreads widen in a stress.")  # fmt: skip
SCENARIO_NOTE = ("Gap: NIFTY's stored overnight gaps of the front future applied at once (no theta earned). Expiry "
                 "day: NIFTY's stored moves on the front contract's own expiry day, applied at the close of the "
                 "book's nearest expiry, the underlying unchanged until that morning. Both with the spot-vol "
                 "response; other underlyings by the worse end of their beta band. Definitions are this app's [U]: "
                 "#394 lists these scenarios without maths.")  # fmt: skip


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _day(d: bh.ScanDay) -> dict[str, Any]:
    return {"start": d.start.isoformat(), "end": d.end.isoformat(), "move": round(d.move, 6),
            "dvol": round(d.dvol, 3), "pnl": round(d.pnl, 2), "mapped": list(d.mapped)}  # fmt: skip


def _scan(x: bh.Scan) -> dict[str, Any]:
    return {"h": x.h, "n": len(x.days), "skipped": x.skipped, "first": x.first and x.first.isoformat(),
            "last": x.last and x.last.isoformat(), "loss_99": _r(x.loss_99), "es": _r(x.es),
            "worst": [_day(d) for d in x.worst], "reasons": list(x.reasons), "es_reasons": list(x.es_reasons),
            "mapped": dict(x.mapped), "flags": list(x.flags)}  # fmt: skip


def _episode(e: bh.EpisodeResult) -> dict[str, Any]:
    pt = lambda p: {"day": p.day.isoformat(), "d": p.d, "move": round(p.move, 6), "dvol": round(p.dvol, 3),  # noqa: E731
                    "pnl": round(p.pnl, 2), "intrinsic": p.intrinsic, "mapped": list(p.mapped)}  # fmt: skip
    return {"key": e.episode.key, "label": e.episode.label, "window": [e.episode.start.isoformat(),
            e.episode.end.isoformat()], "source": e.episode.source, "start": e.start and e.start.isoformat(),
            "path": [pt(p) for p in e.path], "worst": None if e.worst is None else pt(e.worst),
            "candidates": e.candidates, "reasons": list(e.reasons)}  # fmt: skip


def _shock(x: bh.Shock) -> dict[str, Any]:
    return {"kind": x.kind, "label": x.label, "move": _r(x.move, 6), "at": x.at.isoformat(), "pnl": _r(x.pnl),
            "n": x.n, "reasons": list(x.reasons)}  # fmt: skip


def liquidity(states: Sequence[bs.LegState], hs: Mapping[tuple[str, date, str, float], float],
              extra: Sequence[str]) -> dict[str, Any]:  # fmt: skip
    """The stressed liquidity term (unknown until P1.1) and today's quoted exit cost (None if any leg has no
    two-sided quote)."""
    reasons = list(extra)
    cost = 0.0
    for s in states:
        x = s.leg
        h = hs.get((x.underlying, x.expiry, x.right, x.strike))
        if h is None:
            what = "a future (the chain quotes no futures)" if x.right == "future" else "no two-sided quote"
            reasons.append(f"{x.underlying} {x.expiry:%d-%b-%Y} {x.right} {x.strike:g}: {what}")
        else:
            cost += abs(x.qty) * h
    return {"stressed": None, "reason": LIQ_REASON, "quoted_exit_cost": None if reasons else round(cost, 2),
            "reasons": reasons, "note": LIQ_NOTE}  # fmt: skip


def load(underlyings: Sequence[str], today: date) -> tuple[Any, str | None]:
    """The stored history (fnohist.book_history.load), or (None, why) when it cannot be read."""
    from finresearch.db import session_scope
    from finresearch.fnohist import book_history as lh

    try:
        with session_scope() as s:
            return lh.load(s, underlyings, today), None
    except Exception as e:
        return None, f"the stored futures and IV30 history could not be read ({type(e).__name__})"


def es_block(sc: bh.Scan, limit: float | None) -> dict[str, Any]:
    """The book's ES: historical-simulation ES97.5 of the one-session scan, against the book loss limit."""
    why = list(sc.reasons or sc.es_reasons)
    if limit is None:
        why.append("F&O capital is not set on the profile: no book limit to hold the ES to")
    return {"value": _r(sc.es), "h": sc.h, "n": len(sc.days), "limit": limit,
            "ratio": None if sc.es is None or not limit else round(sc.es / limit, 4), "reasons": why,
            "note": bh.ES_NOTE}  # fmt: skip


def history(legs: Sequence[bs.BookLeg], m: bs.Market, holidays: frozenset[date] | set[date], limit: float | None,
            extra: Sequence[str] = ()) -> dict[str, Any]:  # fmt: skip
    """Every history block of the view. `extra`: why the whole book is unknown (unread Groww positions)."""
    from finresearch.fincalc.dates import to_ist

    unders = sorted({x.underlying.upper() for x in legs})
    hist, why = (None, None) if extra or not legs else load(unders, to_ist(m.now).date())
    stop = list(extra) or ([why] if why else [])
    if stop:
        scans = [bh._unknown(h, stop) for h in SCAN_H]
        eps = [bh.EpisodeResult(e, None, (), None, 0, tuple(stop)) for e in bh.EPISODES]
        gaps = [bh.Shock("gap", lb, None, m.now, None, tuple(stop)) for lb, _ in bh.POINTS]
        exp = [bh.Shock("expiry_day", lb, None, m.now, None, tuple(stop)) for lb, _ in bh.POINTS]
        span = None
    elif not legs:  # nothing open: every scenario is exactly 0, nothing to load
        scans = [bh.Scan(h, (), 0, 0.0, 0.0, ()) for h in SCAN_H]
        eps, gaps, exp, span = [], [], [], None
    else:
        series = hist.series  # type: ignore[union-attr]
        scans = [bh.scan(legs, m, series, h, holidays) for h in SCAN_H]
        eps = [bh.episode(legs, m, series, e, holidays) for e in bh.EPISODES]
        gaps = bh.gap_scenarios(legs, m, hist.gaps)  # type: ignore[union-attr]
        exp = bh.expiry_day_scenarios(legs, m, hist.expiry_returns)  # type: ignore[union-attr]
        span = hist.span  # type: ignore[union-attr]
    one = next(x for x in scans if x.h == ES_H)
    return {"stored": None if span is None else [span[0].isoformat(), span[1].isoformat()],
            "scan": [_scan(x) for x in scans], "es": es_block(one, limit), "episodes": [_episode(e) for e in eps],
            "episode_h": bh.EPISODE_H, "gaps": [_shock(x) for x in gaps], "expiry_day": [_shock(x) for x in exp],
            "scenario_note": SCENARIO_NOTE, "skew_note": bs.SKEW_NOTE}  # fmt: skip

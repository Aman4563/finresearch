"""The F&O book's scenario view (#394): today's open paper and Groww legs, priced on the live option chain of each
underlying and expiry, then `fincalc.book_scenarios` (exposures, the full-revaluation grid, the single-day replays and
reverse stress). Analysis only: nothing here places, modifies or cancels an order. Local only: the F&O book is
personal data and never reaches a model.

Marks come from the chain of the leg's own expiry: the mid of a two-sided quote, else the last price (flagged as an
LTP), else unknown. The forward is the put-call parity forward at the strike nearest the underlying that has both a
call and a put quoted (mids first; LTPs, flagged, when no strike has two mids); the chain carries no futures price,
so the same-expiry future is not read yet. Unknown marks or forwards leave the book's figures incomplete, never 0.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc.dates import to_ist, years_to_expiry

RATE = 0.065  # the rate the book's greeks use (fnobook.service.RATE)


def _quote(q: Any) -> tuple[float, str] | None:
    """(price, "mid" | "ltp") of one chain quote, or None."""
    if q is None:
        return None
    bid, ask = (None if v is None else float(v) for v in (q.bid, q.ask))
    if bid and ask and bid > 0 and ask >= bid:
        return (bid + ask) / 2, "mid"
    return (float(q.last_price), "ltp") if q.last_price and q.last_price > 0 else None


def marks(chain: Any) -> dict[tuple[str, float], tuple[float, str]]:
    out: dict[tuple[str, float], tuple[float, str]] = {}
    for r in chain.rows if chain is not None else []:
        for right, q in (("call", r.call), ("put", r.put)):
            got = _quote(q)
            if got is not None:
                out[(right, float(r.strike))] = got
    return out


def forward(chain: Any, now: datetime, rate: float) -> tuple[float | None, str]:
    """The parity forward of a chain and where it came from (or why it is unknown)."""
    if chain is None:
        return None, "the option chain could not be read"
    t = years_to_expiry(chain.expiry, now)
    if t <= 0:
        return None, "the expiry has passed"
    mk = marks(chain)
    ref = float(chain.underlying) if chain.underlying else None
    strikes = sorted({k for _, k in mk}, key=lambda k: (abs(k - ref) if ref else 0.0, k))
    for kind in ("mid", "ltp"):
        for k in strikes:
            c, p = mk.get(("call", k)), mk.get(("put", k))
            if c and p and (kind == "ltp" or c[1] == p[1] == "mid"):
                f = bs.parity_forward(k, c[0], p[0], t, rate)
                if f > 0:
                    src = "mids" if kind == "mid" else "last prices (no strike had two mids)"
                    return f, f"put-call parity at {k:g} from {src}"
    return None, "no strike has both a call and a put quoted, so the parity forward is unknown"


def paper_legs() -> list[bs.BookLeg]:
    """Open paper trades' legs (signed units), marks to be filled from the chain."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import PaperStrategy

    out: list[bs.BookLeg] = []
    with session_scope() as s:
        for st in s.scalars(
            select(PaperStrategy).where(PaperStrategy.status != "closed").order_by(PaperStrategy.id)
        ):
            for x in st.legs:
                q = x.qty if x.side == "buy" else -x.qty
                out.append(bs.BookLeg(st.symbol.upper(), st.expiry, x.right, float(x.strike), q, None, None,
                                      f"paper #{st.id}"))  # type: ignore[arg-type]  # fmt: skip
    return out


async def groww_legs(today: date) -> tuple[list[bs.BookLeg] | None, str | None]:
    """Groww F&O legs through the open-book reader (#358); None when they are unknown (connected but unread or
    unresolved), with the reason."""
    from finresearch.signals.tradeplan import real_positions

    real, why = await real_positions(today)
    if real is None:
        return None, why
    if any(p.expiry is None for p in real):
        return (
            None,
            "A Groww F&O position has no known expiry: the book cannot be repriced (sync Groww again).",
        )
    # a future's Leg.strike is its average price (real_positions): not a strike, so 0 here
    legs = [
        bs.BookLeg(p.symbol.upper(), p.expiry, x.right, 0.0 if x.right == "future" else x.strike, x.qty, None, None,  # type: ignore[arg-type]
                   "groww")
        for p in real
        for x in p.legs
    ]  # fmt: skip
    return legs, why


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _cell(c: bs.Cell) -> dict[str, Any]:
    return {"move": c.move, "dvol": c.dvol, "horizon": c.horizon, "at": c.at.isoformat(), "pnl": _r(c.pnl),
            "floored": c.floored, "intrinsic": c.intrinsic}  # fmt: skip


def _exposure(e: bs.Exposure | None) -> dict[str, float] | None:
    return None if e is None else {k: round(getattr(e, k), 2) for k in ("rs_delta", "rs_gamma_1pct", "vega_pt",
                                                                          "theta_day")}  # fmt: skip


async def _priced(legs: list[bs.BookLeg], now: datetime, sources: Any, rate: float) -> tuple[Any, ...]:
    """The legs with their marks from each (underlying, expiry)'s chain, and the Market of parity forwards; also the
    forwards with their sources. Betas are not measured yet: only NIFTY's (1) is known."""
    from finresearch.fnobook import service

    chains = {k: await service._chain(sources, *k) for k in sorted({(x.underlying, x.expiry) for x in legs})}
    fws = {k: forward(c, now, rate) for k, c in chains.items()}
    mk = {k: marks(c) for k, c in chains.items()}
    priced = [bs.BookLeg(x.underlying, x.expiry, x.right, x.strike, x.qty, *(mk[(x.underlying, x.expiry)].get(
        (x.right, x.strike)) or (None, None)), x.label) for x in legs]  # fmt: skip
    nifty = next(
        (float(c.underlying) for (u, _), c in chains.items() if u == "NIFTY" and c and c.underlying), None
    )
    return priced, bs.Market(now, rate, {k: f for k, (f, _) in fws.items()}, {}, nifty), fws


async def book_shock(now: datetime, *, sources: Any = None, rate: float = RATE,
                     holidays: set[date] | None = None) -> tuple[dict[float, float] | None, list[str]]:  # fmt: skip
    """P4.2 (#395): the open book's (paper and Groww) P&L at each registered NIFTY shock move one session from now,
    every leg repriced on its chain as on the scenarios page. (None, why) when a leg, a forward, a beta or the Groww
    positions are unknown. Reads chains (market-data caches only); writes no record."""
    from finresearch.fnobook import service

    sources = sources or service._sources()
    if holidays is None:
        from finresearch.adapters.groww_market import _holidays

        holidays = _holidays()
    legs = paper_legs()
    real, why = await groww_legs(to_ist(now).date())
    notes = [why] if why else []
    if real is None:
        return None, notes
    legs += real
    priced, m = (await _priced(legs, now, sources, rate))[:2] if legs else ([], bs.Market(now, rate, {}))
    got, reasons = bs.shock_pnls(priced, m, bs.horizon_clock(now, 1, holidays))
    return got, notes + list(reasons)


async def view(now: datetime, *, sources: Any = None, capital: float | None = None,
               rate: float = RATE, holidays: set[date] | None = None) -> dict[str, Any]:  # fmt: skip
    """Everything GET /api/fno/scenarios shows. Reads chains (market-data caches only); writes no record. The Groww
    legs come through `signals.tradeplan.real_positions` (Groww's market is `adapters.groww_market.MARKET`)."""
    from finresearch.fnobook import service

    sources = sources or service._sources()
    if holidays is None:
        from finresearch.adapters.groww_market import _holidays

        holidays = _holidays()
    notes: list[str] = []
    legs = paper_legs()
    real, why = await groww_legs(to_ist(now).date())
    if why:
        notes.append(why)
    unknown_book = real is None
    legs += real or []
    priced, m, fws = await _priced(legs, now, sources, rate)
    states = bs.price_legs(priced, m)
    ex, g = bs.exposures(priced, m), bs.grid(priced, m, holidays=holidays)
    reps = bs.replays(priced, m, holidays)
    extra = (why,) if unknown_book and why else ()
    if extra:  # the Groww positions are unknown: every book figure is incomplete
        g = bs.Grid(tuple(bs.Cell(c.move, c.dvol, c.horizon, c.at, None, extra) for c in g.cells), extra + g.reasons,
                    g.flags)  # fmt: skip
    limit = bs.book_limit(capital) if capital else None
    rev = bs.reverse_stress(g, limit) if limit else []
    return {
        "as_of": to_ist(now).isoformat(), "rate": rate, "notes": notes,
        "legs": [{"underlying": s.leg.underlying, "expiry": s.leg.expiry.isoformat(), "right": s.leg.right,
                  "strike": s.leg.strike, "qty": s.leg.qty, "mark": _r(s.leg.mark), "mark_kind": s.leg.mark_kind,
                  "iv": _r(s.iv, 6), "forward": _r(s.forward), "source": s.leg.label, "reason": s.reason}
                 for s in states],
        "forwards": [{"underlying": u, "expiry": e.isoformat(), "forward": _r(f, 4), "source": src}
                     for (u, e), (f, src) in fws.items()],
        "exposures": {"complete": ex.complete and not extra, "reasons": [*extra, *ex.reasons],
                      "total": None if extra else _exposure(ex.total), "flags": list(ex.flags),
                      "nifty_delta": None if extra else _r(ex.nifty_delta),
                      "nifty_delta_reasons": [*extra, *ex.nifty_delta_reasons],
                      "rows": [{"underlying": r.underlying, "bucket": r.bucket, "legs": r.legs,
                                "exposure": _exposure(r.exposure)} for r in ex.rows]},
        "grid": {"complete": g.complete, "reasons": list(g.reasons), "moves": list(bs.MOVES),
                 "vol_shifts": list(bs.VOL_SHIFTS), "cells": [_cell(c) for c in g.cells],
                 "vol_response": {"down_per_pct": bs.DOWN_SLOPE, "up_per_pct": -bs.UP_SLOPE}},
        "replays": [{"day": r.replay.day.isoformat(), "label": r.replay.label, "move": r.replay.move,
                     "iv30_change": r.replay.iv30_change, "source": r.replay.source,
                     "pnl": None if extra else _r(r.cell.pnl), "reasons": [*extra, *r.cell.reasons],
                     "notes": list(r.notes)} for r in reps],
        "reverse": {"limit": limit, "reason": None if limit else "F&O capital is not set on the profile: no book limit",
                    "rows": [{"horizon": r.horizon, "complete": r.complete, "reasons": list(r.reasons),
                              "cell": None if r.cell is None else _cell(r.cell)} for r in rev]},
    }  # fmt: skip

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

from finresearch.fincalc import beta as fbeta
from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc.dates import to_ist, years_to_expiry

RATE = 0.065  # the rate the book's greeks use (fnobook.service.RATE)
BETA_RULE = ("Each underlying other than NIFTY moves by its beta x NIFTY's move: an EWMA estimate from stored front-"
             "future settles (evals/experiments/book_betas/PREREG.md). Every P&L takes, per underlying, the worse of "
             "its band's two ends (lo = beta - 1.645 SE; hi = beta + 1.645 SE, at least 1), not the worst over the "
             "interval. The NIFTY-equivalent delta uses the point estimate.")  # fmt: skip


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


def half_spreads(chain: Any) -> dict[tuple[str, float], float]:
    """(ask - bid) / 2 of each two-sided quote of a chain (none for a one-sided or crossed quote)."""
    out: dict[tuple[str, float], float] = {}
    for r in chain.rows if chain is not None else []:
        for right, q in (("call", r.call), ("put", r.put)):
            if q is not None and q.bid and q.ask and float(q.bid) > 0 and float(q.ask) >= float(q.bid):
                out[(right, float(r.strike))] = (float(q.ask) - float(q.bid)) / 2
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
            "floored": c.floored, "intrinsic": c.intrinsic, "ends": dict(c.ends)}  # fmt: skip


def _exposure(e: bs.Exposure | None) -> dict[str, float] | None:
    return None if e is None else {k: round(getattr(e, k), 2) for k in ("rs_delta", "rs_gamma_1pct", "vega_pt",
                                                                          "theta_day")}  # fmt: skip


async def _priced(legs: list[bs.BookLeg], now: datetime, sources: Any, rate: float) -> tuple[Any, ...]:
    """The legs with their marks from each (underlying, expiry)'s chain, and the Market of parity forwards and each
    underlying's registered beta estimate (fnohist.betas: stored futures settles; NIFTY = 1); also the forwards with
    their sources."""
    from finresearch.fnobook import service
    from finresearch.fnohist import betas

    chains = {k: await service._chain(sources, *k) for k in sorted({(x.underlying, x.expiry) for x in legs})}
    fws = {k: forward(c, now, rate) for k, c in chains.items()}
    mk = {k: marks(c) for k, c in chains.items()}
    priced = [bs.BookLeg(x.underlying, x.expiry, x.right, x.strike, x.qty, *(mk[(x.underlying, x.expiry)].get(
        (x.right, x.strike)) or (None, None)), x.label) for x in legs]  # fmt: skip
    nifty = next(
        (float(c.underlying) for (u, _), c in chains.items() if u == "NIFTY" and c and c.underlying), None
    )
    est = betas.read({x.underlying for x in legs}, to_ist(now).date()) if legs else {}
    hs = {(u, e, right, k): h for (u, e), c in chains.items() for (right, k), h in half_spreads(c).items()}
    return priced, bs.Market(now, rate, {k: f for k, (f, _) in fws.items()}, est, nifty), fws, hs


async def book_shock(now: datetime, *, sources: Any = None, rate: float = RATE, holidays: set[date] | None = None,
                     betas_out: dict[str, Any] | None = None) -> tuple[dict[float, float] | None, list[str]]:  # fmt: skip
    """P4.2 (#395): the open book's (paper and Groww) P&L at each registered NIFTY shock move one session from now,
    every leg repriced on its chain as on the scenarios page. (None, why) when a leg, a forward, a beta or the Groww
    positions are unknown. Each underlying other than NIFTY moves by the worse end of its beta band; the estimates
    used are put in `betas_out`. Reads chains and stored futures (market-data caches only); writes no record."""
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
    if betas_out is not None:
        betas_out.update(m.betas)
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
    priced, m, fws, hs = await _priced(legs, now, sources, rate)
    states = bs.price_legs(priced, m)
    ex, g = bs.exposures(priced, m), bs.grid(priced, m, holidays=holidays)
    reps = bs.replays(priced, m, holidays)
    extra = (why,) if unknown_book and why else ()
    if extra:  # the Groww positions are unknown: every book figure is incomplete
        g = bs.Grid(tuple(bs.Cell(c.move, c.dvol, c.horizon, c.at, None, extra) for c in g.cells), extra + g.reasons,
                    g.flags)  # fmt: skip
    limit = bs.book_limit(capital) if capital else None
    rev = bs.reverse_stress(g, limit) if limit else []
    from finresearch.fnobook import history_view as hv

    hist = hv.history(priced, m, holidays, limit, extra)
    liq = hv.liquidity(states, hs, extra)
    return {
        "as_of": to_ist(now).isoformat(), "rate": rate, "notes": notes,
        "legs": [{"underlying": s.leg.underlying, "expiry": s.leg.expiry.isoformat(), "right": s.leg.right,
                  "strike": s.leg.strike, "qty": s.leg.qty, "mark": _r(s.leg.mark), "mark_kind": s.leg.mark_kind,
                  "iv": _r(s.iv, 6), "forward": _r(s.forward), "source": s.leg.label, "reason": s.reason}
                 for s in states],
        # the registered beta to NIFTY of every other underlying in the book (fincalc.beta_specs), with its band
        "betas": [fbeta.as_json(e) for e in m.betas.values()],
        "beta_rule": BETA_RULE,
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
        "history": hist, "liquidity": liq,
    }  # fmt: skip


def stored_forward(
    s: Any, day: date, sym: str, expiry: date, spot: float | None, rate: float = RATE
) -> tuple[float | None, str]:
    """The forward of (sym, expiry) on `day` from the stored bhavcopy (#398): the same-expiry future's settle, else
    put-call parity on the settles of the strike nearest `spot` with both a call and a put, else unknown."""
    from sqlalchemy import select

    from finresearch.db.models import FnoBhav
    from finresearch.fincalc.dates import session_clock

    rows = s.execute(select(FnoBhav.option_type, FnoBhav.strike, FnoBhav.settle).where(
        FnoBhav.day == day, FnoBhav.symbol == sym, FnoBhav.expiry == expiry, FnoBhav.settle.is_not(None))).all()  # fmt: skip
    fut = next((float(v) for k, _, v in rows if k == "XX" and v > 0), None)
    if fut is not None:
        return fut, "the same-expiry future's settle"
    ce = {float(k): float(v) for t, k, v in rows if t == "CE"}
    pe = {float(k): float(v) for t, k, v in rows if t == "PE"}
    both = sorted(set(ce) & set(pe), key=lambda k: (abs(k - spot) if spot else 0.0, k))
    if not both:
        return None, f"{sym} {expiry}: no future and no strike with both a call and a put settle stored"
    k = both[0]
    t = years_to_expiry(expiry, session_clock(day))
    return bs.parity_forward(k, ce[k], pe[k], t, rate), f"put-call parity on the {k:g} settles"


def stored_book(s: Any, day: date, *, rate: float = RATE, expiring_after: date | None = None,
                ) -> tuple[list[bs.BookLeg] | None, bs.Market | None, list[str]]:  # fmt: skip
    """The book as marked on `day`'s evening (fno_mark: paper and Groww, the bhavcopy settle), each leg on
    `stored_forward`, with the registered betas (fnohist.betas) as of `day`; the clock is `day`'s 15:30 IST. With
    `expiring_after`, only legs whose expiry is on or after it. ([] , market, []) when nothing is marked; (None, None,
    why) when a leg's direction is unknown. A forward that cannot be found is a note (its legs price as unknown)."""
    from sqlalchemy import select

    from finresearch.db.models import FnoMark
    from finresearch.fincalc.dates import session_clock
    from finresearch.fnohist import betas

    q = select(FnoMark).where(FnoMark.day == day)
    if expiring_after is not None:
        q = q.where(FnoMark.expiry >= expiring_after)
    marks = s.scalars(q.order_by(FnoMark.id)).all()
    if any(m.qty is None for m in marks):
        return (
            None,
            None,
            ["Groww did not confirm whether a leg is long or short: the book's shock P&L is unknown"],
        )
    fws: dict[tuple[str, date], float | None] = {}
    notes: list[str] = []
    for m in marks:
        key = (m.symbol.upper(), m.expiry)
        if key not in fws:
            fws[key], src = stored_forward(
                s, day, m.symbol, m.expiry, None if m.spot is None else float(m.spot), rate
            )
            if fws[key] is None:
                notes.append(src)
    legs = [bs.BookLeg(m.symbol.upper(), m.expiry, m.right, float(m.strike or 0), m.qty,  # type: ignore[arg-type]
                       None if m.price is None else float(m.price), "settle", m.leg_key) for m in marks]  # fmt: skip
    mk = bs.Market(
        session_clock(day), rate, fws, betas.read({x.underlying for x in legs}, day) if legs else {}
    )
    return legs, mk, notes


def stored_shock(s: Any, day: date, *, holidays: set[date] | None = None,
                 rate: float = RATE) -> tuple[dict[float, float] | None, list[str]]:  # fmt: skip
    """P4.5 (#398): the open book's P&L at each registered NIFTY shock move one session after `day`'s close, from
    stored data only (`stored_book`). No chain is read. (None, why) when a leg's direction, price or forward, or a
    beta, is unknown; nothing open is exactly 0 at every move."""
    from finresearch.fincalc.tradeplan import SHOCK_MOVES

    legs, mk, notes = stored_book(s, day, rate=rate)
    if legs is None or mk is None:
        return None, notes
    if not legs:
        return dict.fromkeys(SHOCK_MOVES, 0.0), []
    got, reasons = bs.shock_pnls(legs, mk, bs.horizon_clock(mk.now, 1, holidays or set()))
    return got, notes + list(reasons)

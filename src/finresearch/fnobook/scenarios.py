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

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from finresearch.fincalc import beta as fbeta
from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc.dates import to_ist, years_to_expiry

# #480 (the owner's decision of 23-Oct-2026): each expiry is discounted at its own tenor's rate from signals.rates,
# asked in one place per path (live: `live_rates`; stored days: `stored_rates`, the curve stored before the day), or
# at the one rate a caller passes; every rate is named ("rate: ..." notes, and per expiry in the view; the last
# fallback is the labelled 6.5 %). Each forward is rolled at its own expiry's carry ln(F/S)/T (`chain_carry`,
# `stored_carry`), never at a rate: an expiry whose carry cannot be read leaves its scenarios unknown.
BETA_RULE = ("Each underlying other than NIFTY moves by its beta x NIFTY's move: an EWMA estimate from stored front-"
             "future settles (evals/experiments/book_betas/PREREG.md). Every P&L takes, per underlying, the worse of "
             "its band's two ends (lo = beta - 1.645 SE; hi = beta + 1.645 SE, at least 1), not the worst over the "
             "interval. The NIFTY-equivalent delta uses the point estimate.")  # fmt: skip


def _tenors(legs: Any, now: datetime) -> dict[date, float]:
    """Each open expiry's T now, in years (the tenor its rate is read at)."""
    return {e: max(years_to_expiry(e, now), 0.0) for e in sorted({x.expiry for x in legs})}


async def live_rates(legs: Any, now: datetime, rate: float | None = None) -> dict[date, dict[str, Any]]:
    """Each expiry's rate now: the caller's one rate, else signals.rates at that expiry's own tenor (labelled
    fallbacks)."""
    from finresearch.signals import rates

    tenors = _tenors(legs, now)
    if rate is not None:
        return dict.fromkeys(tenors, rates.given(rate, "the rate passed by the caller"))
    return {e: await rates.risk_free(t) for e, t in tenors.items()}


def stored_rates(s: Any, day: date, tenors: Mapping[date, float]) -> dict[date, dict[str, Any]]:
    """Each expiry's rate on a stored day: signals.rates at its own tenor from the curve stored before `day`."""
    from finresearch.signals import rates

    return {e: rates.rate_on(s, day, t, before=True) for e, t in tenors.items()}


def _days(es: list[date]) -> str:
    names = [f"{e:%d-%b-%Y}" for e in es]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def rate_notes(infos: Mapping[date, dict[str, Any]]) -> list[str]:
    """ "rate: <signals.rates.label> (the <expiry> expiry)", one note per distinct answer."""
    from finresearch.signals import rates

    groups: dict[str, list[date]] = {}
    for e, info in sorted(infos.items()):
        groups.setdefault(rates.label(info), []).append(e)
    return [
        f"rate: {lab} (the {_days(es)} expir{'y' if len(es) == 1 else 'ies'})" for lab, es in groups.items()
    ]


def carry_notes(carries: Mapping[tuple[str, date], bs.Carry]) -> list[str]:
    """ "carry: ..." for each expiry whose carry is known (an unknown one is a scenario reason)."""
    return [f"carry: {u} {e:%d-%b-%Y}: {c.b:.2%} a year ({c.source})"
            for (u, e), c in sorted(carries.items()) if c.b is not None]  # fmt: skip


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


# [U] calendar days: a stored future's carry older than this is not used live (an ex-dividend date or a rate decision
# may lie in between)
STORED_CARRY_DAYS = 7


def _two_sided(q: Any) -> tuple[float, float] | None:
    """(mid, ask - bid) of a two-sided quote, or None."""
    if q is None or q.bid is None or q.ask is None or not 0 < float(q.bid) <= float(q.ask):
        return None
    return (float(q.bid) + float(q.ask)) / 2, float(q.ask) - float(q.bid)


def chain_carry(chain: Any, now: datetime) -> tuple[float | None, str, bs.Carry]:
    """#480: the expiry's forward and carry from its option chain: the parity regression (fincalc.black76.parity_fit)
    over every strike whose call and put both have a two-sided quote (mids, weighted by their spreads), against the
    chain's underlying. A forward of None (with why) when it is not identified: fewer than three such strikes within
    5 % of the forward, or no negative slope."""
    from finresearch.fincalc import black76 as b76

    if chain is None:
        return (
            None,
            "the option chain could not be read",
            bs.Carry(None, reason="the option chain could not be read"),
        )
    t = years_to_expiry(chain.expiry, now)
    if t <= 0:
        return None, "the expiry has passed", bs.Carry(None, reason="the expiry has passed")
    two = {float(r.strike): (_two_sided(r.call), _two_sided(r.put)) for r in chain.rows}
    fit = b76.parity_fit(
        [b76.ParityQuote(k, c[0], p[0], c[1], p[1]) for k, (c, p) in sorted(two.items()) if c and p], t
    )
    if fit.forward is None:
        why = f"no reliable put-call parity fit on the chain ({fit.reason})"
        return None, why, bs.Carry(None, reason=why)
    src = f"put-call parity fit over {fit.n} strikes' mids"
    spot = float(chain.underlying) if chain.underlying else None
    return fit.forward, src, bs.carry_from(fit.forward, spot, t, f"{src} against the chain's underlying")


def stored_future_carry(s: Any, sym: str, expiry: date, today: date, why: str) -> bs.Carry:
    """#480, live, when the chain gives no carry (`why`): the latest stored same-expiry future's settle (fno_bhav, on
    or before `today`) against the underlying's close stored in the same row, at that day's T; unknown when none is
    stored, it is more than STORED_CARRY_DAYS old or its row has no close."""
    from sqlalchemy import select

    from finresearch.db.models import FnoBhav
    from finresearch.fincalc.dates import session_clock

    row = s.execute(select(FnoBhav.day, FnoBhav.settle, FnoBhav.underlying).where(
        FnoBhav.symbol == sym, FnoBhav.expiry == expiry, FnoBhav.option_type == "XX", FnoBhav.day <= today,
        FnoBhav.settle.is_not(None)).order_by(FnoBhav.day.desc()).limit(1)).first()  # fmt: skip
    if row is None:
        return bs.Carry(None, reason=f"{why}, and no same-expiry future is stored")
    d, settle, close = row
    if (today - d).days > STORED_CARRY_DAYS:
        return bs.Carry(None, reason=f"{why}, and the latest stored same-expiry future ({d:%d-%b-%Y}) is more than "
                                     f"{STORED_CARRY_DAYS} days old")  # fmt: skip
    src = f"the same-expiry future's settle on {d:%d-%b-%Y} (stored bhavcopy) against the underlying's close that day"
    got = bs.carry_from(
        float(settle), None if close is None else float(close), years_to_expiry(expiry, session_clock(d)), src
    )
    return got if got.b is not None else bs.Carry(None, reason=f"{why}; {got.reason}")


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


def _forward_row(
    u: str, e: date, f: float | None, src: str, c: bs.Carry, info: dict[str, Any]
) -> dict[str, Any]:
    """One expiry in the view (#480): its forward, the carry its scenarios roll it at (or why it is unknown), the
    dividend yield q = r - b that carry implies at the expiry's own rate, and that rate."""
    from finresearch.fincalc.black76 import implied_q

    r = float(info["rate"])
    q = None if c.b is None else implied_q(r, c.forward, c.spot, c.t)  # type: ignore[arg-type]
    return {"underlying": u, "expiry": e.isoformat(), "forward": _r(f, 4), "source": src, "spot": c.spot,
            "t_years": c.t, "carry": c.b, "carry_source": c.source,
            "carry_reason": None if c.b is not None else (c.reason or bs.NO_CARRY), "implied_q": q, "rate": r,
            "rate_info": info}  # fmt: skip


async def _priced(legs: list[bs.BookLeg], now: datetime, sources: Any,
                  rinfo: Mapping[date, dict[str, Any]]) -> tuple[Any, ...]:  # fmt: skip
    """The legs with their marks from each (underlying, expiry)'s chain, and the Market: each expiry's forward and
    carry (`chain_carry`; without a fit, `forward` at the expiry's own rate prices the legs and the carry is
    `stored_future_carry`'s), its rate (`rinfo`, from `live_rates`) and each underlying's registered beta estimate
    (fnohist.betas: stored futures settles; NIFTY = 1); also the forwards with their sources."""
    from finresearch.fnobook import service
    from finresearch.fnohist import betas

    today = to_ist(now).date()
    chains = {k: await service._chain(sources, *k) for k in sorted({(x.underlying, x.expiry) for x in legs})}
    rs = {e: float(i["rate"]) for e, i in rinfo.items()}
    fws: dict[tuple[str, date], tuple[float | None, str]] = {}
    carries: dict[tuple[str, date], bs.Carry] = {}
    for (u, e), c in chains.items():
        f, src, carries[(u, e)] = chain_carry(c, now)
        fws[(u, e)] = (f, src) if f is not None else forward(c, now, rs[e])
    if need := [k for k, c in carries.items() if c.b is None and fws[k][0] is not None]:
        from finresearch.db import session_scope

        try:
            with session_scope() as s:
                for u, e in need:
                    carries[(u, e)] = stored_future_carry(
                        s, u, e, today, carries[(u, e)].reason or bs.NO_CARRY
                    )
        except Exception as ex:  # the store cannot be read: the carry stays unknown, with why
            for k in need:
                carries[k] = bs.Carry(None, reason=f"{carries[k].reason}; the stored futures could not be read "
                                                   f"({type(ex).__name__})")  # fmt: skip
    mk = {k: marks(c) for k, c in chains.items()}
    priced = [bs.BookLeg(x.underlying, x.expiry, x.right, x.strike, x.qty, *(mk[(x.underlying, x.expiry)].get(
        (x.right, x.strike)) or (None, None)), x.label) for x in legs]  # fmt: skip
    nifty = next(
        (float(c.underlying) for (u, _), c in chains.items() if u == "NIFTY" and c and c.underlying), None
    )
    est = betas.read({x.underlying for x in legs}, today) if legs else {}
    hs = {(u, e, right, k): h for (u, e), c in chains.items() for (right, k), h in half_spreads(c).items()}
    m = bs.Market(
        now, next(iter(rs.values()), 0.0), {k: f for k, (f, _) in fws.items()}, est, nifty, carries, rs
    )
    return priced, m, fws, hs


async def book_shock(now: datetime, *, sources: Any = None, rate: float | None = None, holidays: set[date] | None = None,
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
    if not legs:  # nothing open: nothing is priced, so no rate is read
        got, reasons = bs.shock_pnls([], bs.Market(now, rate or 0.0, {}), bs.horizon_clock(now, 1, holidays))
        return got, notes + list(reasons)
    rinfo = await live_rates(legs, now, rate)
    priced, m = (await _priced(legs, now, sources, rinfo))[:2]
    notes += rate_notes(rinfo) + carry_notes(m.carries)
    if betas_out is not None:
        betas_out.update(m.betas)
    got, reasons = bs.shock_pnls(priced, m, bs.horizon_clock(now, 1, holidays))
    return got, notes + list(reasons)


async def view(now: datetime, *, sources: Any = None, capital: float | None = None,
               rate: float | None = None, holidays: set[date] | None = None) -> dict[str, Any]:  # fmt: skip
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
    rinfo = await live_rates(legs, now, rate)
    priced, m, fws, hs = await _priced(legs, now, sources, rinfo)
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
        "as_of": to_ist(now).isoformat(), "notes": notes,
        "legs": [{"underlying": s.leg.underlying, "expiry": s.leg.expiry.isoformat(), "right": s.leg.right,
                  "strike": s.leg.strike, "qty": s.leg.qty, "mark": _r(s.leg.mark), "mark_kind": s.leg.mark_kind,
                  "iv": _r(s.iv, 6), "forward": _r(s.forward), "source": s.leg.label, "reason": s.reason}
                 for s in states],
        # the registered beta to NIFTY of every other underlying in the book (fincalc.beta_specs), with its band
        "betas": [fbeta.as_json(e) for e in m.betas.values()],
        "beta_rule": BETA_RULE,
        "forwards": [_forward_row(u, e, f, src, m.carry(u, e), rinfo[e]) for (u, e), (f, src) in fws.items()],
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
    s: Any, day: date, sym: str, expiry: date, spot: float | None, rate: float
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


def stored_carry(s: Any, day: date, sym: str, expiry: date, spot: float | None,
                 rate: float) -> tuple[float | None, str, bs.Carry]:  # fmt: skip
    """#480: the forward of (sym, expiry) on `day` and its carry ln(F/S)/T against the underlying's close `spot` (else
    the close stored with that day's rows): the same-expiry future's settle (as `stored_forward`); without one, the
    parity regression (fincalc.black76.parity_fit) over the day's market settles (fincalc.fno_history.market_settle),
    whose forward then prices the legs; else `stored_forward`'s single-strike parity at `rate` prices them and the
    carry is unknown."""
    from sqlalchemy import select

    from finresearch.db.models import FnoBhav
    from finresearch.fincalc import black76 as b76
    from finresearch.fincalc.dates import session_clock
    from finresearch.fincalc.fno_history import market_settle

    rows = s.execute(select(FnoBhav.option_type, FnoBhav.strike, FnoBhav.settle, FnoBhav.close, FnoBhav.contracts,
                            FnoBhav.underlying).where(FnoBhav.day == day, FnoBhav.symbol == sym,
                            FnoBhav.expiry == expiry, FnoBhav.settle.is_not(None))).all()  # fmt: skip
    t = years_to_expiry(expiry, session_clock(day))
    spot = spot or next((float(u) for *_, u in rows if u is not None and u > 0), None)
    fut = next((float(v) for k, _, v, *_ in rows if k == "XX" and v > 0), None)
    if fut is not None:
        return (
            fut,
            "the same-expiry future's settle",
            bs.carry_from(
                fut,
                spot,
                t,
                f"the same-expiry future's settle on {day:%d-%b-%Y} against the underlying's close",
            ),
        )
    px = {(k, float(x)): float(v) for k, x, v, c, n, _ in rows
          if k in ("CE", "PE") and market_settle(float(v), None if c is None else float(c), n)}  # fmt: skip
    both = sorted({x for _, x in px if ("CE", x) in px and ("PE", x) in px})
    fit = b76.parity_fit([b76.ParityQuote(x, px[("CE", x)], px[("PE", x)]) for x in both], t)
    if fit.forward is not None:
        src = f"put-call parity fit over {fit.n} strikes' market settles"
        return fit.forward, src, bs.carry_from(fit.forward, spot, t, f"{src} on {day:%d-%b-%Y}")
    f, src = stored_forward(s, day, sym, expiry, spot, rate)
    why = f"no same-expiry future is stored and no reliable put-call parity fit over the market settles ({fit.reason})"
    return f, src, bs.Carry(None, reason=why, forward=f, spot=spot, t=t)


FIXED_RULE = "a registered caller's fixed rate: every forward rolled at it (stored_book's `rate`)"


def stored_book(s: Any, day: date, *, rate: float | None = None, expiring_after: date | None = None,
                ) -> tuple[list[bs.BookLeg] | None, bs.Market | None, list[str]]:  # fmt: skip
    """The book as marked on `day`'s evening (fno_mark: paper and Groww, the bhavcopy settle), with the registered
    betas (fnohist.betas) as of `day`; the clock is `day`'s 15:30 IST. With `expiring_after`, only legs whose expiry is
    on or after it. ([] , market, []) when nothing is marked; (None, None, why) when a leg's direction is unknown. A
    forward that cannot be found is a note (its legs price as unknown).
    `rate` None (#480): each expiry at its own rate (`stored_rates`, named in "rate: " notes; the labelled 6.5 % when no
    curve is stored), its forward and carry from `stored_carry` (known carries named in "carry: " notes).
    `rate` given: a registered caller's fixed rule (joint-risk-v1, fincalc.joint_pnl.RATE): that one rate for every
    expiry, `stored_forward` at it, every forward rolled at it, and no note."""
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
    clock = session_clock(day)
    if not marks:  # nothing is priced, so no rate is read (and none is used)
        return [], bs.Market(clock, 0.0 if rate is None else rate, {}, {}), []
    legs = [bs.BookLeg(m.symbol.upper(), m.expiry, m.right, float(m.strike or 0), m.qty,  # type: ignore[arg-type]
                       None if m.price is None else float(m.price), "settle", m.leg_key) for m in marks]  # fmt: skip
    est = betas.read({x.underlying for x in legs}, day)
    syms = {m.symbol.upper(): m.symbol for m in marks}
    spots = {m.symbol.upper(): float(m.spot) for m in reversed(marks) if m.spot is not None}
    keys = sorted({(m.symbol.upper(), m.expiry) for m in marks})
    fws: dict[tuple[str, date], float | None] = {}
    notes: list[str] = []
    # a registered caller's rule, exactly as before #480 (each key's first mark's close picks the parity strike)
    if rate is not None:
        for m in marks:
            key = (m.symbol.upper(), m.expiry)
            if key not in fws:
                fws[key], src = stored_forward(
                    s, day, m.symbol, m.expiry, None if m.spot is None else float(m.spot), rate
                )
                if fws[key] is None:
                    notes.append(src)
        return legs, bs.Market(clock, rate, fws, est).fixed_carry(rate, FIXED_RULE), notes
    rinfo = stored_rates(s, day, {e: max(years_to_expiry(e, clock), 0.0) for _, e in keys})
    carries: dict[tuple[str, date], bs.Carry] = {}
    unknown = []
    for u, e in keys:
        fws[(u, e)], src, carries[(u, e)] = stored_carry(s, day, syms[u], e, spots.get(u), rinfo[e]["rate"])
        if fws[(u, e)] is None:
            unknown.append(src)
    notes = rate_notes(rinfo) + carry_notes(carries) + unknown
    rs = {e: float(i["rate"]) for e, i in rinfo.items()}
    return legs, bs.Market(clock, rs[keys[0][1]], fws, est, carries=carries, rates=rs), notes


def stored_shock(s: Any, day: date, *,
                 holidays: set[date] | None = None) -> tuple[dict[float, float] | None, list[str]]:  # fmt: skip
    """P4.5 (#398): the open book's P&L at each registered NIFTY shock move one session after `day`'s close, from
    stored data only (`stored_book`). No chain is read. (None, why) when a leg's direction, price or forward, or a
    beta, is unknown; nothing open is exactly 0 at every move."""
    from finresearch.fincalc.tradeplan import SHOCK_MOVES

    legs, mk, notes = stored_book(s, day)
    if legs is None or mk is None:
        return None, notes
    if not legs:
        return dict.fromkeys(SHOCK_MOVES, 0.0), []
    got, reasons = bs.shock_pnls(legs, mk, bs.horizon_clock(mk.now, 1, holidays or set()))
    return got, notes + list(reasons)


def stored_es(s: Any, day: date, *, holidays: set[date] | None = None) -> tuple[float | None, str]:
    """#394 -> #398: the open book's historical ES97.5 one session after `day`'s close (fincalc.book_history.scan over
    every stored start day, fnohist.book_history), from stored data only (`stored_book`). (None, why) when a leg, a
    forward, a beta or the history is unknown, or there are too few start days; (0.0, ...) when nothing is open."""
    from finresearch.fincalc import book_history as bh
    from finresearch.fnohist import book_history as lh

    legs, mk, notes = stored_book(s, day)
    if legs is None or mk is None:
        return None, "; ".join(notes)
    if not legs:
        return 0.0, "nothing open"
    hist = lh.load(s, sorted({x.underlying for x in legs}), day)
    sc = bh.scan(legs, mk, hist.series, 1, holidays or set())
    if sc.es is None:
        return None, "; ".join([*notes, *(sc.reasons or sc.es_reasons)])
    mapped = ", ".join(f"{u} beta-mapped on {n} days" for u, n in sc.mapped.items() if n)
    return sc.es, (f"historical ES97.5 ₹{sc.es:,.0f} of tonight's marked book one session ahead over "
                   f"{len(sc.days)} start days ({sc.first} to {sc.last}; {sc.skipped} skipped)"
                   + (f"; {mapped}" if mapped else "") + "".join(f"; {n}" for n in notes))  # fmt: skip

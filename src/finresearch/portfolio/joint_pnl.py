"""The stored daily joint P&L (#397 (b)): one append-only `joint_pnl_day` row per IST session, written by the monitor
(monitor.joint_pnl) and read by the pre-registered forward test (evals/experiments/joint_risk/PREREG.md, ADDENDUM.md).
The arithmetic is `fincalc.joint_pnl`; this module reads the stored rows and writes one row.

* The session from the last stored evening (`prev_day`) to `day`: the holdings' actual from the canonical value
  history (portfolio.series), the book's actual from the fno_mark settles, NIFTY's move from index_eod; explained by
  the previous evening's stored holdings × their own returns (the benchmark-built history's closes) and the previous
  evening's marked legs revalued at NIFTY's realised move (L(m)'s F&O term, from stored data only).
* The evening snapshot of `day`: D_total, L(m) on book_scenarios.MOVES and each holding's value and beta, which the
  next session is explained from.

Unknown never becomes 0: a part that cannot be computed leaves the row unusable with the reason. The real (Groww)
book is known only when that evening's marks job finished with Groww read that day or not connected. Analysis only:
nothing here places an order. Local only: both books are personal data and never reach a model.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import AlertEvalSlot, FnoMark, IndexEod, JointPnlDay
from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc import joint_pnl as jp
from finresearch.fincalc import joint_risk as jr
from finresearch.fincalc.dates import session_clock


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


def _dec(x: float | None, nd: int = 2) -> Decimal | None:
    return None if x is None else Decimal(str(round(x, nd)))


# --------------------------------------------------------------------------- reads
def book_known(s: Session, day: date) -> str | None:
    """None when `day`'s marks cover the whole book, else why not: the marks job has not finished, Groww's positions
    were not read that day, or a Groww contract was not resolved. Groww not connected = no real book (known)."""
    row = s.get(AlertEvalSlot, f"fno_marks:{day.isoformat()}")
    r = (row.result or {}) if row is not None else {}
    if r.get("status") != "done":
        return f"the F&O marks of {day:%d-%b-%Y} were not written (monitor.fno_marks)"
    note = r.get("real_note")
    if note and "not_connected" not in note:
        return f"the Groww book on {day:%d-%b-%Y} is unknown: {note}"
    if r.get("real_unresolved"):
        return f"{r['real_unresolved']} Groww position(s) on {day:%d-%b-%Y} not in Groww's instruments file"
    return None


def marks(s: Session, day: date) -> list[jp.Mark]:
    rows = s.scalars(select(FnoMark).where(FnoMark.day == day).order_by(FnoMark.id))
    return [jp.Mark(m.leg_key, m.symbol.upper(), m.expiry, m.right, _f(m.strike), m.qty, _f(m.price), _f(m.spot),
                    _f(m.iv), _f(m.t_years)) for m in rows]  # fmt: skip


def _closes(s: Session, lo: date, hi: date) -> dict[date, float]:
    q = select(IndexEod.day, IndexEod.close).where(IndexEod.symbol == "NIFTY", IndexEod.day.between(lo, hi),
                                                   IndexEod.close.is_not(None))  # fmt: skip
    return {d: float(c) for d, c in s.execute(q)}


def own_returns(hist: Any, keys: list[str], prev: date, day: date) -> dict[str, float | None]:
    """Each holding's own return from `prev`'s close to `day`'s (closes or NAVs, split/bonus factors on (prev, day])."""
    out: dict[str, float | None] = {}
    for k in keys:
        c = (getattr(hist, "closes", None) or {}).get(k) or {}
        a, b = c.get(prev), c.get(day)
        f = 1.0
        for d, x in ((getattr(hist, "factors", None) or {}).get(k) or {}).items():
            if prev < d <= day:
                f *= x
        out[k] = b * f / a - 1 if a and b else None
    return out


def _series_day(ser: Any, day: date) -> jp.SeriesDay | None:
    if ser is None or day not in ser.days:
        return None
    i = ser.days.index(day)
    return jp.SeriesDay(day, ser.value[i], ser.invested[i], ser.complete[i])


# --------------------------------------------------------------------------- the session
NO_PREV = (
    "no previous evening's snapshot is stored (the first stored day): nothing to explain the session from"
)


def _unknown(day: date, prev: date | None, why: str) -> jp.Day:
    n = (None, [why])
    return jp.day(day, prev or day, nifty_move=None, holdings=n, fno=n, own=n, beta=None, model=n, market=n)


def session(s: Session, prev: JointPnlDay | None, day: date, hist: Any) -> jp.Day:
    """The session from the last stored evening to `day`, explained from that evening's stored snapshot."""
    from finresearch.fnobook.scenarios import stored_book
    from finresearch.portfolio import series

    if prev is None:
        return _unknown(day, None, NO_PREV)
    p = prev.day
    ser, ser_why = series.load(s)
    closes = _closes(s, p, day)
    between = sorted(
        {d for d in closes if p < d < day} | {d for d in (ser.days if ser else []) if p < d < day}
    )
    if between:
        return _unknown(day, p, f"the last stored evening is {p:%d-%b-%Y} but {between[0]:%d-%b-%Y} was a session "
                                "in between: not one session's P&L")  # fmt: skip
    move = closes[day] / closes[p] - 1 if p in closes and day in closes else None
    holdings = (
        (None, [ser_why]) if ser is None else jp.holdings_actual(_series_day(ser, p), _series_day(ser, day))
    )
    if holdings[0] is None and ser is not None and _series_day(ser, day) is None:
        holdings = (None, [f"the value history does not reach {day:%d-%b-%Y} yet"])
    why = book_known(s, p) or book_known(s, day)
    fno = (None, [why]) if why else jp.fno_actual(marks(s, p), marks(s, day), day)
    market = (None, [why]) if why else jp.fno_market(marks(s, p), marks(s, day), day)
    if prev.holdings is None:
        own: jp.Part = (None, ["the previous evening's holdings are unknown", *prev.snapshot_reasons])
    else:
        snap = [jp.Snap(h["key"], float(h["value"]), h.get("beta")) for h in prev.holdings]
        own = jp.own_move(snap, own_returns(hist, [h.key for h in snap], p, day))
    pwhy = book_known(s, p)
    if pwhy:
        model: jp.Part = (None, [pwhy])
    elif move is None:
        model = (None, ["NIFTY's move is unknown"])
    else:
        legs, mk, notes = stored_book(
            s, p, rate=jp.RATE, expiring_after=day
        )  # the registered rate (see snapshot)
        got = (
            jp.fno_model(legs, mk, session_clock(day), move)
            if legs is not None and mk is not None
            else (None, [])
        )
        model = got if got[0] is not None else (None, [*got[1], *notes])
    return jp.day(day, p, nifty_move=move, holdings=holdings, fno=fno, own=own,
                  beta=jp.beta_move(_f(prev.d_holdings), move), model=model, market=market)  # fmt: skip


# --------------------------------------------------------------------------- the evening snapshot
def snapshot(s: Session, day: date, hist: Any, holidays: set[date] | frozenset[date]) -> dict[str, Any]:
    """D_total, L(m) on book_scenarios.MOVES and each holding's value and beta at `day`'s evening (the column values)."""
    from finresearch.fnobook.scenarios import stored_book
    from finresearch.portfolio import joint_risk as jl

    hs: jr.Holdings
    if hist is None or not getattr(hist, "ok", False) or not hist.days or hist.days[-1] != day:
        hs = f"the value history with NIFTYBEES (for betas) does not reach {day:%d-%b-%Y}"
    else:
        hs = jl.holdings(hist)
    clock = session_clock(day)
    why = book_known(s, day)
    legs, mk, notes = (
        # #366/#385: the registered joint_risk forward test (PREREG.md) keeps its fixed 6.5 % (fincalc.joint_pnl.RATE,
        # the rate fno_mark's IVs were implied at), not the dated FBIL rate: a dated rate needs a registered variant
        (None, None, [why])
        if why
        else stored_book(s, day, rate=jp.RATE, expiring_after=day + timedelta(days=1))
    )
    b = jr.Book(legs, mk or bs.Market(clock, jp.RATE, {}), None if legs is not None else "; ".join(notes))
    d = jr.d_total(hs, b)
    ls = jr.losses(hs, b, bs.horizon_clock(clock, 1, holidays), bs.MOVES)
    reasons = list(dict.fromkeys([*d.reasons, *(r for x in ls for r in x.reasons)]))
    r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {"d_holdings": _dec(d.holdings), "d_fno": _dec(d.fno), "d_total": _dec(d.total),
            "l_curve": [{"move": x.move, "holdings": r2(x.holdings), "fno": r2(x.fno), "total": r2(x.total)}
                        for x in ls],
            "holdings": None if isinstance(hs, str) else [{"key": h.key, "value": round(h.value, 2),
                                                            "beta": None if h.beta is None else round(h.beta, 6)}
                                                           for h in hs],
            "snapshot_reasons": reasons}  # fmt: skip


def record(s: Session, day: date, hist: Any, *, holidays: set[date] | frozenset[date]) -> dict[str, Any]:
    """Write `day`'s row unless it exists (never updated). Returns counts only (no rupee figure: the result goes to
    alert_eval_slot)."""
    prev = s.scalars(
        select(JointPnlDay).where(JointPnlDay.day < day).order_by(JointPnlDay.day.desc()).limit(1)
    ).first()
    x = session(s, prev, day, hist)
    v = {"day": day, "prev_day": None if prev is None else prev.day, "nifty_move": _dec(x.nifty_move, 8),
         "holdings_actual": _dec(x.holdings_actual), "fno_actual": _dec(x.fno_actual), "actual": _dec(x.actual),
         "holdings_own": _dec(x.holdings_own), "fno_model": _dec(x.fno_model), "explained": _dec(x.explained),
         "holdings_beta": _dec(x.holdings_beta), "fno_market": _dec(x.fno_market), "usable": x.usable,
         "reasons": list(x.reasons), "notes": list(x.notes), **snapshot(s, day, hist, holidays)}  # fmt: skip
    got = s.scalars(insert(JointPnlDay).values(**v).on_conflict_do_nothing(index_elements=["day"])
                    .returning(JointPnlDay.id)).first()  # fmt: skip
    return {"written": got is not None, "usable": x.usable, "reasons": len(x.reasons),
            "snapshot_complete": v["d_total"] is not None}  # fmt: skip

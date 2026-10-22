"""The event-volatility panel (P3.8, #391): implied event moves from the latest stored end-of-day term structure
(fno_iv_eod's near and next expiry), the 'IV elevated by event' flag and the history of realised event moves
(index_eod closes), all from the point-in-time event calendar (#367). Display only: nothing here reaches the card's
probabilities, which keep their registered specs.

Clock: business time (fincalc.bustime, the registered prior weights, event variance EXCLUDED so it is not counted
twice) when an NSE holiday list covers the expiries, else the calendar clock (days / 365); each figure says which.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import FnoIvEod, IndexEod
from finresearch.eventcal import calendar as cal
from finresearch.eventcal import store
from finresearch.fincalc import bustime
from finresearch.fincalc import eventvol as ev
from finresearch.fincalc.dates import ist_datetime, to_ist
from finresearch.fnohist.store import INDEX_OPTIONS

AUTO: Any = object()  # read the cached NSE holiday lists


def _tau(day: date, expiry: date, calendar: bustime.Calendar | None) -> tuple[float, str]:
    if calendar is not None and calendar.covers(day, expiry):
        bt = bustime.business_years(expiry, ist_datetime(day, *bustime.SESSION_CLOSE), calendar, bustime.PRIOR,
                                    events=bustime.EXCLUDED)  # fmt: skip
        if bt.value:
            return bt.value, "business"
    return (expiry - day).days / 365, "calendar"


def _implied(
    row: FnoIvEod, tier1: list[cal.Event], calendar: bustime.Calendar | None
) -> tuple[dict, str, list]:
    """Every tier-1 event in (row.day, next expiry] fitted from the row's two expiries: {event day: Implied}."""
    pts, clock = [], "calendar"
    for exp, iv in ((row.near_expiry, row.near_iv), (row.next_expiry, row.next_iv)):
        if exp is not None and exp > row.day:
            tau, clock = _tau(row.day, exp, calendar)
            pts.append(ev.Point(exp, tau, None if iv is None else float(iv) / 100))
    last = max((p.expiry for p in pts), default=row.day)
    inside = [e for e in tier1 if row.day < cal.india_day(e) <= last]
    return ev.fit(pts, [cal.india_day(e) for e in inside]) if inside else {}, clock, pts


def _closes(s: Session, symbol: str, now: datetime) -> dict[date, float]:
    rows = s.execute(select(IndexEod.day, IndexEod.close).where(IndexEod.symbol == symbol, IndexEod.day <= to_ist(now)
                                                                .date())).all()  # fmt: skip
    return {d: float(c) for d, c in rows if c is not None and ev.close_known(d, now)}


def _imp(i: ev.Implied | None) -> dict[str, Any]:
    if i is None:
        return {"j2": None, "move": None, "expected_abs": None, "median_abs": None, "method": None,
                "reason": "no expiry spans the event"}  # fmt: skip
    return {"j2": i.j2, "move": i.move, "expected_abs": i.expected_abs, "median_abs": i.median_abs,
            "method": i.method, "reason": i.reason}  # fmt: skip


def build(s: Session, symbol: str, now: datetime, *, calendar: Any = AUTO) -> dict[str, Any]:
    sym = symbol.strip().upper()
    if sym not in INDEX_OPTIONS:
        return {"symbol": sym, "status": "not_index",
                "note": "The event calendar holds index-level events only (RBI policy, Budget, elections)."}  # fmt: skip
    events, coverage = store.load(s)
    seen = cal.known_at(events, now)
    tier1 = [e for e in seen if e.tier == 1]
    closes = _closes(s, sym, now)
    out: dict[str, Any] = {"symbol": sym, "status": "ok", "history": _history(s, sym, events, closes, now)}
    rows = s.scalars(select(FnoIvEod).where(FnoIvEod.symbol == sym, FnoIvEod.day <= to_ist(now).date())
                     .order_by(FnoIvEod.day.desc()).limit(2)).all()  # fmt: skip
    row = next((r for r in rows if ev.close_known(r.day, now)), None)  # today's row only after today's close
    if row is None or row.near_expiry is None:
        return {**out, "status": "no_data", "upcoming": [], "note": "No stored end-of-day IV for this index."}
    if calendar is AUTO:
        calendar = bustime.nse_calendar(row.day, row.next_expiry or row.near_expiry)
    pending = [e for e in tier1 if not ev.close_known(cal.india_day(e), now)]
    fits, clock, pts = _implied(row, tier1, calendar)  # every event since the row's close, passed or not
    last = max((p.expiry for p in pts), default=row.day)
    window = cal.window_check(events, coverage, start=row.day, end=last, as_of=now)
    stats = out["history"]
    upcoming = []
    for e in sorted(pending, key=cal.india_day):
        d = cal.india_day(e)
        if not row.day < d <= last:
            continue
        i = fits.get(d)
        st = next((h["_stats"] for h in stats if h["type"] == e.type), None)
        upcoming.append({"type": e.type, "title": e.title, "day": d.isoformat(), "source": e.source_url,
                         **_imp(i), "short_gamma": ev.short_gamma(i or ev.Implied(None), st)})  # fmt: skip
    near = pts[0] if pts else None
    inside = [(u["title"], fits.get(date.fromisoformat(u["day"])) or ev.Implied(None)) for u in upcoming
              if near and date.fromisoformat(u["day"]) <= near.expiry]  # fmt: skip
    elev = ev.elevation(near, inside) if near else {"status": "unknown", "note": "No near expiry."}
    if elev["status"] == "none" and window["status"] != "none":
        elev = {
            **elev,
            "status": "unknown",
            "note": window["note"],
        }  # an uncovered calendar is never "no event"
    for h in stats:
        h.pop("_stats")
    context = [x["title"] + " " + x["india_day"] for x in window["context"]]
    return {**out, "as_of": row.day.isoformat(), "clock": clock,
            "expiries": [{"expiry": p.expiry.isoformat(), "iv": p.sigma, "tau": p.tau} for p in pts],
            "window": {"status": window["status"], "uncovered": window["uncovered"], "note": window["note"]},
            "upcoming": upcoming, "context": context, "elevation": elev,
            "note": "Implied event moves from the stored end-of-day term structure; context events in the same "
                    "window (" + (", ".join(context) or "none") + ") share any premium measured. Informational: "
                    "the card's probabilities do not use it."}  # fmt: skip


def _history(s: Session, sym: str, events: list[cal.Event], closes: dict[date, float],
             now: datetime) -> list[dict[str, Any]]:  # fmt: skip
    """Realised moves by type as known at `now`, each paired with the implied move its last pre-event close showed
    (only from calendar rows public at that close)."""
    hist = ev.history(events, closes, now)
    starts = {m.start for h in hist.values() for m in h.moves if m.start}
    rows = {
        r.day: r for r in s.scalars(select(FnoIvEod).where(FnoIvEod.symbol == sym, FnoIvEod.day.in_(starts)))
    }
    clock = bustime.Calendar.from_sessions(closes)
    out = []
    for kind, st in hist.items():
        moves, pairs = [], []
        for m in st.moves:
            row = rows.get(m.start) if m.start else None
            j2 = None
            if row is not None:
                at = ist_datetime(row.day, *bustime.SESSION_CLOSE)
                t1 = [e for e in cal.known_at(events, at) if e.tier == 1]
                j2 = (
                    _implied(row, t1, clock)[0].get(m.day, ev.Implied(None)).j2
                    if kind in cal.TIER1_TYPES
                    else None
                )
            if m.r is not None and j2 is not None:
                pairs.append((m.r, j2))
            moves.append({"day": m.day.isoformat(), "title": m.title, "r": m.r, "reason": m.reason,
                          "start": m.start and m.start.isoformat(), "end": m.end and m.end.isoformat(),
                          "implied_move": None if j2 is None else j2**0.5})  # fmt: skip
        out.append({"type": kind, "title": cal.TITLES.get(kind, kind), "n": st.n, "median_abs": st.median_abs,
                    "mean_r2": st.mean_r2, "fairness": ev.fairness(pairs), "moves": moves, "_stats": st})  # fmt: skip
    return out

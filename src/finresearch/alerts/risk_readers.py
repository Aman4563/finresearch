"""P4.5 (#398): what the risk alerts read. Each reading names the series it read; an input that cannot be read is
None with the reason (alerts.risk raises a "can't check" alert), never 0 or ok. Local only: the F&O book and the
funds are personal data and never reach a model. Alerts only: nothing here trades.

Book level (`book_readings`, pure): the open book's common-shock P&L (fnobook.scenarios.book_shock, at
fincalc.tradeplan.SHOCK_MOVES one session ahead) against the shock budget (limit_shock, hysteresis) and against the
book loss limit (stress_breach); the margin cap in use (fincalc.tradeplan.margin_utilisation with no new trade, over
MARGIN_UTIL_MAX; only with Groww connected); the book's historical ES97.5 (fnobook.scenarios.stored_es, #394) against
the book loss limit; stressed margin (P4.3) is not built, so while a book is open it reads "can't check" (as does the
ES when the caller passes none). Stored series (`series_readings`, `leg_readings`) are in the database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.alerts.risk import Reading
from finresearch.db.models import AlertEvalSlot, FnoBhav, FnoIvEod, FnoMark
from finresearch.fincalc import risk_alerts as ra
from finresearch.fincalc import tradeplan as tp

Row = tuple[str, str, Reading]  # (rule id, instrument, reading)
SHOCK_SERIES = "the open F&O book (paper + Groww) repriced at fincalc.tradeplan.SHOCK_MOVES one session ahead"
ES_SERIES = (
    "historical ES97.5 of the open F&O book (fno_mark) one session ahead over every stored NIFTY start day "
    "(fincalc.book_history.scan on fno_bhav futures and fno_iv_eod.iv_30d)"
)
FUNDS_SERIES = "Groww funds from the last sync (fnobook.service.stored_funds)"


def book_readings(shock: Mapping[float, float] | None, shock_notes: Sequence[str], capital: float | None,
                  funds: Mapping[str, float] | None, funds_note: str | None, *, book_open: bool,
                  es: tuple[float | None, str] | None = None) -> list[Row]:  # fmt: skip
    """`es`: the book's historical ES97.5 in ₹ (a positive loss) or None, with its detail
    (fnobook.scenarios.stored_es); read against the book loss limit. Without it the ES reads "can't check"."""
    out: list[Row] = []
    why = "; ".join(shock_notes) or "the open book could not be repriced"
    if not capital or capital <= 0:
        miss = "F&O capital is not set on the profile, so the book has no budget to measure against"
        out += [("limit_shock", "BOOK", Reading(None, SHOCK_SERIES, miss)),
                ("stress_breach", "BOOK", Reading(None, SHOCK_SERIES, miss))]  # fmt: skip
    elif shock is None or any(v is None for v in shock.values()):
        out += [("limit_shock", "BOOK", Reading(None, SHOCK_SERIES, why)),
                ("stress_breach", "BOOK", Reading(None, SHOCK_SERIES, why))]  # fmt: skip
    else:
        worst = min(shock, key=lambda m: shock[m])
        loss = max(0.0, -shock[worst])
        rate = "".join(
            f"; {n}" for n in shock_notes if n.startswith(("rate: ", "carry: "))
        )  # #366/#385: which rate priced it; #480: and which carry rolled each forward
        for rid, pct, name in (("limit_shock", tp.SHOCK_BUDGET_PCT, "common-shock budget"),
                               ("stress_breach", tp.BOOK_BUDGET_PCT, "book loss limit")):  # fmt: skip
            limit = capital * pct / 100
            out.append((rid, "BOOK", Reading(loss / limit, SHOCK_SERIES, f"worst loss ₹{loss:,.0f} at a {worst:+.0%} "
                        f"NIFTY move vs the ₹{limit:,.0f} {name} ({pct:g}% of F&O capital){rate}")))  # fmt: skip
    if (book_open or shock is None) and es is not None:
        out.append(("es", "BOOK", _es_reading(es, capital)))
    elif book_open or shock is None:
        out.append(("es", "BOOK", Reading(None, "no expected-shortfall series", "the book's expected shortfall is "
                                          "not computed yet (P3.4), so its limit cannot be checked")))  # fmt: skip
    if book_open or shock is None:
        out += [("margin_stress", "BOOK", Reading(None, "no stressed-margin series", "stressed margin is not "
                                                  "computed yet (P4.3), so the headroom cannot be checked"))]  # fmt: skip
    if funds is not None:
        u = tp.margin_utilisation(funds["margin_used"], 0.0, funds["cash"], funds["collateral"])
        out.append(("margin", "BOOK", Reading(u / tp.MARGIN_UTIL_MAX, FUNDS_SERIES, f"margin in use ₹"
                    f"{funds['margin_used']:,.0f}: utilisation {u:.0%} of a {tp.MARGIN_UTIL_MAX:.0%} cap")))  # fmt: skip
    elif funds_note and "not connected" not in funds_note:
        out.append(("margin", "BOOK", Reading(None, FUNDS_SERIES, funds_note)))
    return out


def _es_reading(es: tuple[float | None, str], capital: float | None) -> Reading:
    value, detail = es
    if not capital or capital <= 0:
        return Reading(
            None, ES_SERIES, "F&O capital is not set on the profile, so the book has no loss limit"
        )
    if value is None:
        return Reading(None, ES_SERIES, detail)
    limit = capital * tp.BOOK_BUDGET_PCT / 100
    return Reading(value / limit, ES_SERIES, f"{detail} vs the ₹{limit:,.0f} book loss limit "
                   f"({tp.BOOK_BUDGET_PCT:g}% of F&O capital)")  # fmt: skip


# --------------------------------------------------------------------------- stored series (database only)
INDEX_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})
MARK_RATE = 0.065  # fnobook.marks.RATE: the rate fno_mark's IVs were implied with (Black-Scholes on the spot)


def _iv_series(s: Session, sym: str, day: date) -> list[tuple[date, float | None]]:
    n = ra.SD_WINDOW + 2 * ra.CUM_SESSIONS
    rows = s.execute(select(FnoIvEod.day, FnoIvEod.iv_30d).where(FnoIvEod.symbol == sym, FnoIvEod.day <= day)
                     .order_by(FnoIvEod.day.desc()).limit(n)).all()  # fmt: skip
    return [(d, None if v is None else float(v)) for d, v in reversed(rows)]


def front(s: Session, sym: str, day: date) -> list[ra.FutureDay]:
    """The front future's sessions of `sym` up to `day` from fno_bhav (same-contract pairs, ra.front_future)."""
    rows = s.execute(select(FnoBhav.day, FnoBhav.expiry, FnoBhav.open, FnoBhav.close)
                     .where(FnoBhav.symbol == sym, FnoBhav.option_type == "XX", FnoBhav.day <= day)).all()  # fmt: skip
    return ra.front_future((d, e, None if o is None else float(o), None if c is None else float(c))
                           for d, e, o, c in rows)  # fmt: skip


def series_readings(s: Session, symbols: Sequence[str], day: date) -> list[Row]:
    """Per underlying: the one-session and 5-session IV30 shocks (fno_iv_eod.iv_30d, the app's Black-76 EOD IV30 from
    NSE's bhavcopy settles: EOD against EOD, like with like) and the overnight gap of the front future (fno_bhav)."""
    out: list[Row] = []
    for sym in symbols:
        ser = f"fno_iv_eod.iv_30d {sym} (EOD IV30 from NSE bhavcopy settles) to {day}"
        ivs = _iv_series(s, sym, day)
        if not ivs or ivs[-1][0] != day:
            last = ivs[-1][0].isoformat() if ivs else "never"
            miss = f"IV30 of {sym} is not stored for {day.isoformat()} (last {last})"
            out += [("iv_shock", sym, Reading(None, ser, miss)), ("cum_shock", sym, Reading(None, ser, miss))]
        else:
            vals = [v for _, v in ivs]
            for rid, st in (("iv_shock", ra.iv_shock(vals)), ("cum_shock", ra.cum_shock(vals))):
                out.append((rid, sym, Reading(st.value, ser, st.detail)))
        gser = f"fno_bhav front future {sym} (open vs the same contract's previous close) to {day}"
        f = front(s, sym, day)
        if not f or f[-1].day != day:
            out.append(
                ("gap", sym, Reading(None, gser, f"the front future of {sym} is not stored for {day}"))
            )
        else:
            st = ra.gap_z(open_=f[-1].open, prev_close=f[-1].prev_close, rets=[x.ret for x in f[:-1]])
            out.append(("gap", sym, Reading(st.value, gser, st.detail)))
    return out


def _leg_key(m: FnoMark) -> str:
    return f"{m.symbol} {m.expiry.isoformat()} {float(m.strike or 0):g}{'PE' if m.right == 'put' else 'CE'}"


def _may_hold(s: Session, day: date) -> bool:
    """Whether the book may hold an option leg on `day`: a paper trade still open, or Groww connected."""
    from finresearch.db.models import BrokerConnection, PaperStrategy

    paper = s.scalar(select(PaperStrategy.id).where(PaperStrategy.status != "closed", PaperStrategy.expiry >= day)
                     .limit(1))  # fmt: skip
    g = s.get(BrokerConnection, "groww")
    return paper is not None or (g is not None and g.enabled)


def leg_readings(s: Session, day: date, now: datetime) -> list[Row]:
    """Per open short option leg in tonight's marks (fno_mark, paper and Groww): its risk-neutral touch probability
    at its own IV (ra.TOUCH_LABEL) and the share of the front future's stored overnight gaps beyond its strike's
    distance; per index underlying held, the tier-1 event check up to its last expiry (eventcal). Tonight's marks
    not written, or a Groww book that could not be marked, read unknown for the whole book."""
    import math

    from finresearch.eventcal import calendar, store

    mser = f"fno_mark {day} (NSE bhavcopy settle, the underlying's close and the leg's own IV)"
    slot = s.get(AlertEvalSlot, f"fno_marks:{day.isoformat()}")
    res = dict(slot.result or {}) if slot is not None else {}
    why = None
    if res.get("status") != "done":
        why = f"tonight's F&O marks (monitor.fno_marks) are not written for {day}"
    elif (note := res.get("real_note")) and "not_connected" not in note:
        why = f"the Groww book was not marked: {note}"
    elif res.get("real_unresolved"):
        why = f"{res['real_unresolved']} Groww position(s) could not be resolved to a contract"
    if why and res.get("status") != "done" and not _may_hold(s, day):
        return []  # no open paper trade and Groww not connected: the book is known to be empty
    if why:
        return [("touch", "BOOK", Reading(None, mser, why)), ("gap_stop", "BOOK", Reading(None, mser, why))]
    marks = s.scalars(select(FnoMark).where(FnoMark.day == day).order_by(FnoMark.id)).all()
    out: list[Row] = []
    gaps: dict[str, list[float]] = {}
    for m in marks:
        if m.right not in ("call", "put") or (m.qty is not None and m.qty >= 0):
            continue
        key = _leg_key(m)
        if m.qty is None:
            miss = "Groww did not confirm whether this leg is long or short"
            out += [("touch", key, Reading(None, mser, miss)), ("gap_stop", key, Reading(None, mser, miss))]
            continue
        spot, k = (None if m.spot is None else float(m.spot)), float(m.strike or 0)
        if m.iv is None or spot is None or m.t_years is None:
            out.append(
                (
                    "touch",
                    key,
                    Reading(None, mser, m.note or "the leg's IV or the underlying's close is unknown"),
                )
            )
        else:
            t, iv = float(m.t_years), float(m.iv)
            f = spot * math.exp(MARK_RATE * t)
            p = ra.touch_prob(f, k, iv, t)
            out.append(("touch", key, Reading(p, mser, f"strike {k:g}, forward {f:,.0f}, IV {iv:.1%}, "
                                              f"{t * 365:.1f} days: {ra.TOUCH_LABEL}")))  # fmt: skip
        gser = f"fno_bhav front-future overnight gaps of {m.symbol} to {day}"
        if spot is None:
            out.append(("gap_stop", key, Reading(None, gser, "the underlying's close is unknown")))
            continue
        if m.symbol not in gaps:
            gaps[m.symbol] = [x.gap for x in front(s, m.symbol, day) if x.gap is not None]
        dist = abs(math.log(k / spot))
        p, n = ra.gap_tail(gaps[m.symbol], dist)
        detail = (f"{n} stored gaps, fewer than the {ra.MIN_GAPS} needed" if p is None else
                  f"{p:.1%} of {n} stored overnight gaps moved more than the {dist:.2%} to strike {k:g}")  # fmt: skip
        out.append(("gap_stop", key, Reading(p, gser, detail)))
    held: dict[str, date] = {}
    for m in marks:
        if m.symbol in INDEX_UNDERLYINGS:
            held[m.symbol] = max(held.get(m.symbol, m.expiry), m.expiry)
    if held:
        try:
            ev, cov = store.load(s)
        except Exception:
            ev, cov = None, None
        for sym, end in sorted(held.items()):
            w = calendar.window_check(ev, cov, start=day, end=end, as_of=now)
            val = {"events": 1.0, "none": 0.0}.get(w["status"])
            names = ", ".join(f"{e['title']} {e['day']}" for e in w["tier1"])
            out.append(("event", sym, Reading(val, f"calendar_event tier 1 from {day} to {end}",
                                              w["note"] + (f" ({names})" if names else ""))))  # fmt: skip
    return out

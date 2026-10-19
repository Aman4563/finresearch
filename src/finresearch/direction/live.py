"""The direction model on the live history (#327): today's probabilities, their status, the forecast ledger.

state(symbol): features at the latest stored session (the app's own F&O history, loaded nightly after NSE publishes
the bhavcopy), every model's probability per horizon and event (the logistic refitted on every outcome known by that
session, exactly as the walk-forward did), and each cell's status from the latest stored evaluation run whose
registration hash matches the code (none: "not evaluated", treated as no edge). The probability SHOWN is the
logistic's only for an accepted cell; otherwise the market-implied one, labelled "market-implied, no edge".

log(symbol, now): one forecast per underlying × horizon × event for the latest session, logged to the forecast ledger
(signals.ledger) under the shown method, plus the logistic as a "shadow" forecast while it is not accepted. Allowed
only between the session's close and the next session's open (09:15 IST): a forecast logged later would know part of
the next session's move. Each carries `start_date` (the session) so the ledger counts non-overlapping windows as
independent events, and the threshold (K·σ_h) fixed at logging time.

Resolution (registered with signals.ledger.resolver for every horizon × event): the bhavcopy's underlying value on the
h-th stored session after the start date; waits (NotYet) while any weekday in between has no bhavcopy manifest row
(neither loaded nor a confirmed no-file day), so a missed download never shifts the count.
"""

from __future__ import annotations

import math
import time as _time
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from finresearch.direction import data, evaluate, models, specs
from finresearch.direction.features import build_features, sigma_h
from finresearch.signals import ledger

METHOD_IMPLIED = "F&O direction: market-implied lognormal (30-day IV), no edge [direction v1]"
METHOD_LOGIT = "F&O direction: L2 logistic, monthly walk-forward [direction v1]"
SOURCE = "signal:fno_direction"
OPEN_IST = time(9, 15)
CACHE_SECONDS = 1800
_CACHE: dict[tuple[str, str | None], tuple[float, dict[str, Any]]] = {}


def event_kind(event: str, h: int) -> str:
    return f"fno_dir_{event}_h{h}"


def event_words(symbol: str, event: str, h: int) -> str:
    return f"{symbol} {specs.EVENT_TEXT[event]} {h} session{'s' if h != 1 else ''} after the logged session's close"


# --------------------------------------------------------------------------- evaluation runs (fno_direction_run)
def save_run(s, report: dict[str, Any]) -> int:
    from finresearch.db.models import FnoDirectionRun

    run = FnoDirectionRun(version=report["version"], registration_hash=report["registration_hash"],
                          registered_hash=report["registered_hash"], oos_evaluated=report["oos_evaluated"],
                          source=report["source"][:200], verdict=report["verdict"], report=report)  # fmt: skip
    s.add(run)
    s.flush()
    return run.id


def latest_run(s, *, matching: bool = True):
    """The newest out-of-sample run (with the current registration hash when `matching`)."""
    from sqlalchemy import select

    from finresearch.db.models import FnoDirectionRun
    from finresearch.direction import prereg

    q = select(FnoDirectionRun).where(FnoDirectionRun.oos_evaluated.is_(True))
    if matching:
        q = q.where(FnoDirectionRun.registration_hash == prereg.HASH)
    return s.scalars(
        q.order_by(FnoDirectionRun.created_at.desc(), FnoDirectionRun.id.desc()).limit(1)
    ).first()


def run_summary(run, symbol: str | None = None) -> dict[str, Any] | None:
    """The stored report without the per-day forecasts, cells of `symbol` only when given."""
    if run is None:
        return None
    rep = run.report
    cells = [{k: v for k, v in c.items() if k != "predictions_oos"} for c in rep.get("cells", [])
             if symbol is None or c.get("symbol") == symbol]  # fmt: skip
    return {"id": run.id, "created_at": run.created_at.isoformat() if run.created_at else None,
            **{k: rep.get(k) for k in ("version", "registration_hash", "registered_hash", "registered_on", "source",
                                       "generated_at", "windows", "criteria", "k", "verdict", "accepted", "primary")},
            "underlying": (rep.get("underlyings") or {}).get(symbol) if symbol else rep.get("underlyings"),
            "cells": cells}  # fmt: skip


def statuses(run, symbol: str) -> dict[tuple[int, str], str]:
    if run is None:
        return {}
    return {(c["horizon"], c["event"]): c.get("status", "no_edge") for c in run.report.get("cells", [])
            if c.get("symbol") == symbol}  # fmt: skip


# --------------------------------------------------------------------------- today's probabilities
def compute(rows, feats, symbol: str, status: dict[tuple[int, str], str]) -> dict[str, Any]:
    """Every model's probability at the last row (pure: the tests call it on synthetic rows)."""
    i = len(rows) - 1
    last = rows[i]
    horizons = []
    for h in specs.HORIZONS:
        evs = {}
        for ev in specs.EVENTS:
            y = evaluate.labels(rows, h, ev)
            tr = evaluate.train_set(y, rows, h, i)
            fit = None
            if len(tr) >= specs.LOGIT.min_train and len({y[j] for j in tr}) == 2:
                fit = models.fit_logit([feats[j] for j in tr], [y[j] for j in tr], h)  # type: ignore[misc]
            known = [int(y[j]) for j in range(len(rows)) if j + h <= i and y[j] is not None]  # type: ignore[arg-type]
            p = {"implied": models.implied_p(last.iv_30, h, ev), "base_rate": models.base_rate_p(known),
                 "logistic": None if fit is None else fit.predict(feats[i])}  # fmt: skip
            st = status.get((h, ev), "not_evaluated")
            use_logit = st == "accepted" and p["logistic"] is not None
            evs[ev] = {**{k: None if v is None else round(v, 4) for k, v in p.items()}, "status": st,
                       "shown": p["logistic"] if use_logit else p["implied"],
                       "shown_model": "logistic" if use_logit else "implied",
                       "label": "model (accepted out of sample)" if use_logit else "market-implied, no edge",
                       "train_n": len(tr), "text": specs.EVENT_TEXT[ev]}  # fmt: skip
            if evs[ev]["shown"] is not None:
                evs[ev]["shown"] = round(evs[ev]["shown"], 4)
        sig = sigma_h(last.iv_30, h)
        horizons.append({"h": h, "sigma_pct": None if sig is None else round(sig * 100, 3),
                         "threshold_pct": None if sig is None else round(specs.K * sig * 100, 3), "events": evs})  # fmt: skip
    accepted = [
        f"h={h['h']} {ev}" for h in horizons for ev, v in h["events"].items() if v["status"] == "accepted"
    ]
    up5 = next(h for h in horizons if h["h"] == 5)["events"]["up"]
    return {"symbol": symbol, "as_of": last.day.isoformat(), "spot": last.extra.get("underlying") or last.close,
            "iv_30": last.iv_30, "features": {k: None if v is None else round(v, 5) for k, v in feats[i].items()},
            "horizons": horizons, "accepted": accepted,
            "model_status": "accepted" if accepted else "no_edge",
            "headline": {"h": 5, "p_up": up5["shown"], "label": up5["label"],
                         "call_or_put": None if not accepted else ("CALL" if (up5["shown"] or 0.5) > 0.5 else "PUT")}}  # fmt: skip


def state(symbol: str, url: str | None = None, *, fresh: bool = False) -> dict[str, Any]:
    """compute() on the stored history, with the latest matching evaluation run (cached CACHE_SECONDS)."""
    from finresearch.db import session_scope

    sym = symbol.upper()
    hit = _CACHE.get((sym, url))
    if hit and not fresh and hit[0] > _time.time() - CACHE_SECONDS:
        return hit[1]
    rows, notes = data.load(sym, url)
    if len(rows) < 260:
        raise LookupError(f"only {len(rows)} stored sessions of {sym}: the model needs a year of F&O history "
                          "(finresearch fno backfill)")  # fmt: skip
    feats = build_features(rows, check_every=0)
    with session_scope() as s:
        run = latest_run(s)
        status = statuses(run, sym)
        summary = run_summary(run, sym)
    out = compute(rows, feats, sym, status) | {"run": summary, "data": notes}
    _CACHE[(sym, url)] = (_time.time(), out)
    return out


# --------------------------------------------------------------------------- the ledger
def next_session(day: date, holidays: set[date] | None) -> date:
    from finresearch.fincalc.dates import next_business_day

    return next_business_day(day, holidays or set())


def session_after(day: date, n: int, holidays: set[date] | None) -> date:
    d = day
    for _ in range(n):
        d = next_session(d, holidays)
    return d


def logging_window(as_of: date, now: datetime, holidays: set[date] | None) -> tuple[bool, str]:
    """(allowed, why): from the as-of session's close until the next session opens (IST)."""
    from finresearch.fincalc.dates import IST, to_ist

    ist = to_ist(now)
    close = datetime.combine(as_of, time(15, 30), tzinfo=IST)
    opens = datetime.combine(next_session(as_of, holidays), OPEN_IST, tzinfo=IST)
    if ist < close:
        return False, f"the {as_of:%d-%b-%Y} session has not closed yet"
    if ist >= opens:
        return False, (f"the latest stored close is {as_of:%d-%b-%Y} and the next session opened at "
                       f"{opens:%d-%b %H:%M} IST: a forecast from that close now would know part of the next move. "
                       "The nightly job logs each session's forecast after NSE publishes the bhavcopy.")  # fmt: skip
    return True, "ok"


def log(symbol: str, now: datetime | None = None, *, st: dict[str, Any] | None = None,
        holidays: set[date] | None = None) -> dict[str, Any]:  # fmt: skip
    """Log the latest session's forecasts (module docstring). Returns what was logged or why nothing was."""
    from finresearch.signals.base import Signal, Validation

    now = now or datetime.now(UTC)
    if holidays is None:
        from finresearch.adapters.nse_holidays import trading_holidays

        holidays = trading_holidays()
    st = st or state(symbol)
    as_of = date.fromisoformat(st["as_of"])
    ok, why = logging_window(as_of, now, holidays)
    if not ok:
        return {"logged": [], "skipped": why, "as_of": st["as_of"]}
    spot0 = st.get("spot")
    if not spot0:
        return {"logged": [], "skipped": "no underlying value stored for the session", "as_of": st["as_of"]}
    out = []
    for hz in st["horizons"]:
        h = hz["h"]
        resolve_on = session_after(as_of, h, holidays)
        for ev, v in hz["events"].items():
            sig = sigma_h(st["iv_30"], h)
            if ev != "up" and sig is None:
                continue
            base_inputs = {"symbol": st["symbol"], "start_date": st["as_of"], "h": h, "event": ev, "spot0": spot0,
                           "iv_30": st["iv_30"], "threshold_logret": None if sig is None else specs.K * sig,
                           "k": specs.K, "status": v["status"]}  # fmt: skip
            methods = [("implied", METHOD_IMPLIED, "rule_based")]
            if v["status"] == "accepted":
                methods = [("logistic", METHOD_LOGIT, "backtested")]
            elif v["logistic"] is not None:
                methods.append(("logistic", METHOD_LOGIT, "shadow"))
            for key, method, vstatus in methods:
                p = v[key]
                if p is None:
                    continue
                sigl = Signal(asset="fno", instrument=st["symbol"], name=f"{st['symbol']} direction", action="INFORMATIONAL",
                              score=0.0, event=event_words(st["symbol"], ev, h), horizon=f"{h} session{'s' if h > 1 else ''}",
                              method=method, validation=Validation(vstatus), probability=float(p))  # fmt: skip
                fid = ledger.record(sigl, source=SOURCE, resolve_on=resolve_on, event_kind=event_kind(ev, h),
                                    inputs={**base_inputs, "model": key}, created_at=now)  # fmt: skip
                out.append(
                    {
                        "id": fid,
                        "h": h,
                        "event": ev,
                        "model": key,
                        "p": p,
                        "resolve_on": resolve_on.isoformat(),
                    }
                )
    return {"logged": out, "skipped": None, "as_of": st["as_of"]}


def _underlying_on(s, symbol: str, day: date) -> float | None:
    from sqlalchemy import func, select

    from finresearch.db.models import FnoBhav, FnoIvEod

    v = s.scalar(select(FnoIvEod.underlying).where(FnoIvEod.symbol == symbol, FnoIvEod.day == day))
    if v is None:
        v = s.scalar(select(func.max(FnoBhav.underlying)).where(FnoBhav.symbol == symbol, FnoBhav.day == day))
    return None if v is None else float(v)


async def resolve_direction(f, deps: Any, session) -> ledger.Resolution:
    from sqlalchemy import select

    from finresearch.db.models import FnoBhav, FnoBhavDay

    inp = f.inputs or {}
    sym, h, ev = inp["symbol"], int(inp["h"]), inp["event"]
    start = date.fromisoformat(inp["start_date"])
    spot0, thr = float(inp["spot0"]), inp.get("threshold_logret")
    days = list(session.scalars(select(FnoBhav.day).distinct().where(FnoBhav.symbol == sym, FnoBhav.day > start)
                                .order_by(FnoBhav.day).limit(h)))  # fmt: skip
    if len(days) < h:
        raise ledger.NotYet(f"{len(days)} of {h} sessions after {start} stored for {sym}")
    end = days[-1]
    manifest = {
        d: st for d, st in session.execute(select(FnoBhavDay.day, FnoBhavDay.status).where(FnoBhavDay.day.between(start, end)))
    }  # fmt: skip
    d = start + timedelta(days=1)
    while d < end:
        if d.weekday() < 5 and d not in days and manifest.get(d) not in ("loaded", "no_file"):
            raise ledger.NotYet(f"no bhavcopy record for {d}: cannot be sure {end} is the {h}th session")
        d += timedelta(days=1)
    s1 = _underlying_on(session, sym, end)
    if s1 is None:
        raise ledger.NotYet(f"no underlying value stored for {sym} on {end}")
    r = math.log(s1 / spot0)
    if ev == "up":
        out = int(r > 0)
    elif thr is None:
        return ledger.Resolution(None, None, "void: no threshold was stored with the forecast")
    else:
        out = int(r > thr) if ev == "up_k" else int(r < -thr)
    words = f" (threshold ±{thr * 100:.2f} %)" if thr is not None and ev != "up" else ""
    return ledger.Resolution(out, r * 100, f"{sym} {spot0:,.2f} on {start} → {s1:,.2f} on {end} ({h} sessions): "
                             f"{r * 100:+.2f} %{words}; source: NSE bhavcopy underlying value")  # fmt: skip


for _h in specs.HORIZONS:
    for _ev in specs.EVENTS:
        ledger.resolver("fno", event_kind(_ev, _h))(resolve_direction)


# --------------------------------------------------------------------------- live accuracy from the ledger
def live_accuracy(s, symbol: str) -> list[dict[str, Any]]:
    """Per method × horizon × event: logged, open, resolved and the scored summary over independent events."""
    from sqlalchemy import select

    from finresearch.db.models import Forecast
    from finresearch.evals.calibration import summarize

    rows = list(s.scalars(select(Forecast).where(Forecast.asset == "fno", Forecast.instrument == symbol.upper(),
                                                  Forecast.source == SOURCE)))  # fmt: skip
    groups: dict[tuple[str, str], list] = {}
    for f in rows:
        groups.setdefault((f.method, f.event_kind), []).append(f)
    out = []
    for (method, kind), fs in sorted(groups.items()):
        scored_rows = [
            f for f in fs if f.status == "resolved" and f.probability is not None and f.outcome is not None
        ]
        ind = ledger.independent_events(scored_rows)
        sm = summarize([f.probability for f in ind], [int(f.outcome) for f in ind], 5).to_json()
        inp = fs[0].inputs or {}
        out.append({"method": method, "event_kind": kind, "h": inp.get("h"), "event": inp.get("event"),
                    "model": inp.get("model"), "logged": len(fs), "open": sum(f.status == "open" for f in fs),
                    "resolved": len(scored_rows), "independent": len(ind), "void": sum(f.status == "void" for f in fs),
                    "summary": sm})  # fmt: skip
    return out

"""The direction model on the database (#327): the loader (read only), today's probabilities through the API, the
decision with and without an accepted model, ledger logging (its time window and dedupe) and automatic resolution,
the nightly monitor step. Test database only, synthetic bhavcopy rows (tests/direction_synth.py); the option chain is
test_fno_signal's recorded NIFTY chain; every other HTTP request fails the test."""

from __future__ import annotations

import math
from datetime import date, datetime

import direction_synth as ds
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, text
from test_fno_signal import FakeFno

from finresearch.fincalc import dates

TABLES = (
    "fno_bhav, fno_bhav_day, fno_iv_eod, fno_participant_oi, fno_direction_run, forecast, alert_eval_slot"
)
AS_OF = date(2026, 9, 28)  # a Monday; the recorded chain is from 29-Sep-2026 10:00 IST
IST = dates.IST


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    from finresearch.adapters import http
    from finresearch.fnobook import sources

    class NoSources:
        async def board_meetings(self, symbol):
            raise AssertionError("no network in tests")

    async def refuse(self, *a, **kw):
        raise AssertionError(f"network request in an offline test: {a[:1]}")

    monkeypatch.setattr(http.PoliteClient, "get", refuse)
    monkeypatch.setattr(sources, "SOURCES", NoSources())
    monkeypatch.setattr("finresearch.adapters.nse_holidays.trading_holidays", lambda *a, **k: set())


@pytest.fixture
def db(env):
    from finresearch.db import session_scope
    from finresearch.direction import live

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    live._CACHE.clear()
    yield env
    live._CACHE.clear()
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES} CASCADE"))


def store(rows, symbol: str = "NIFTY") -> None:
    """DayRows as the #306 tables hold them: a future, one call and one put (their OI), the derived IV row, the
    manifest."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay, FnoIvEod

    bhav, iv, man = [], [], []
    for r in rows:
        bhav.append(dict(day=r.day, symbol=symbol, instrument="IDF", expiry=r.fut_expiry, strike=0, option_type="XX",
                         high=r.fut_high, low=r.fut_low, close=r.fut_close, settle=r.fut_close, oi=int(r.fut_oi),
                         contracts=100, underlying=round(r.close, 2)))  # fmt: skip
        for right, oi in (("CE", r.call_oi), ("PE", r.put_oi)):
            bhav.append(dict(day=r.day, symbol=symbol, instrument="IDO", expiry=r.near_expiry, strike=20000,
                             option_type=right, close=100, settle=100, oi=int(oi), contracts=50,
                             underlying=round(r.close, 2)))  # fmt: skip
        iv.append(dict(symbol=symbol, day=r.day, iv_30d=round(r.iv_30, 3), near_iv=r.near_iv, next_iv=r.next_iv,
                       skew_25d=round(r.skew_25d, 3), underlying=round(r.close, 2), rate=0.065))  # fmt: skip
        man.append(dict(day=r.day, status="loaded"))
    with session_scope() as s:
        s.execute(insert(FnoBhav), bhav)
        s.execute(insert(FnoIvEod), iv)
        have = set(s.scalars(select(FnoBhavDay.day)))
        if [m for m in man if m["day"] not in have]:
            s.execute(insert(FnoBhavDay), [m for m in man if m["day"] not in have])


def panel(end: date = AS_OF, start: date = date(2025, 6, 2), seed: int = 5):
    return ds.panel(start, end, seed=seed, iv=14.0)


@pytest.fixture
def client(db, monkeypatch):
    from finresearch.api import create_app

    monkeypatch.setattr(dates, "now_ist", lambda: datetime(2026, 9, 29, 10, tzinfo=IST))
    clock = {"now": datetime(2026, 9, 28, 20, 0, tzinfo=IST)}
    with TestClient(create_app(fno_client=FakeFno, clock=lambda: clock["now"])) as c:
        c.clock = clock  # type: ignore[attr-defined]
        yield c


def test_loader_reads_the_stored_history_read_only(db):
    from sqlalchemy.exc import InternalError

    from finresearch.direction import data

    rows = panel()
    store(rows)
    got, notes = data.load("NIFTY")
    assert [r.day for r in got] == [r.day for r in rows]
    assert got[0].ret is None and all(r.ret_source == "underlying" for r in got[1:])
    assert got[-1].ret == pytest.approx(math.log(round(rows[-1].close, 2) / round(rows[-2].close, 2)))
    assert got[-1].iv_30 == pytest.approx(rows[-1].iv_30, abs=1e-3)
    assert got[-1].put_oi == int(rows[-1].put_oi) and got[-1].near_expiry == rows[-1].near_expiry
    assert notes["index_eod"] is False and notes["participant_days"] == 0
    with data.read_only() as s, pytest.raises(InternalError):
        s.execute(text("DELETE FROM fno_bhav"))


def test_api_without_an_evaluation_shows_market_implied_and_no_trade(client):
    store(panel())
    r = client.get("/api/fno/direction/NIFTY")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["as_of"] == AS_OF.isoformat() and d["model_status"] == "no_edge" and d["run"] is None
    for hz in d["horizons"]:
        for v in hz["events"].values():
            assert (
                v["status"] == "not_evaluated"
                and v["shown_model"] == "implied"
                and v["shown"] == v["implied"]
            )
            assert v["label"] == "market-implied, no edge"
    dec = d["decision"]
    assert (
        dec["status"] == "ok" and dec["action"] == "NO TRADE" and dec["label"] == "NO TRADE: no measured edge"
    )
    assert dec["non_directional"]["suggest_api"] == "/api/fno/NIFTY/suggest" and dec["plan"] is None
    assert dec["horizon"] == 5 and dec["expiry"] == "2026-10-06" and len(dec["candidates"]) == 6
    assert d["headline"]["call_or_put"] is None and d["live_accuracy"] == []
    reg = client.get("/api/fno/direction/registration").json()
    assert reg["matches_code"] is True and reg["spec"]["horizons"] == [1, 5, 10, 20]
    assert client.get("/api/fno/direction/NOPE").status_code == 404


def test_a_stored_run_sets_each_cell_status(client):
    from finresearch.db import session_scope
    from finresearch.direction import live, prereg, specs

    store(panel())
    cells = [{"symbol": "NIFTY", "horizon": h, "event": ev, "status": "accepted" if (h, ev) == (5, "up") else "no_edge",
              "oos": {"n": 500}, "predictions_oos": [[1, 2]]} for h in specs.HORIZONS for ev in specs.EVENTS]  # fmt: skip
    rep = {"version": specs.SPEC_VERSION, "registration_hash": prereg.HASH, "registered_hash": prereg.HASH,
           "oos_evaluated": True, "source": "synthetic", "verdict": "accepted: NIFTY h=5 up", "cells": cells,
           "underlyings": {"NIFTY": {"sessions": 1}}, "accepted": ["NIFTY h=5 up"]}  # fmt: skip
    with session_scope() as s:
        live.save_run(s, rep)
    d = client.get("/api/fno/direction/NIFTY?decide=false").json()
    up5 = next(h for h in d["horizons"] if h["h"] == 5)["events"]["up"]
    assert (
        up5["status"] == "accepted" and up5["shown_model"] == "logistic" and up5["shown"] == up5["logistic"]
    )
    assert d["model_status"] == "accepted" and d["headline"]["call_or_put"] in ("CALL", "PUT")
    assert d["run"]["verdict"].startswith("accepted") and "predictions_oos" not in d["run"]["cells"][0]
    # a run under another registration hash is ignored
    with session_scope() as s:
        s.execute(text("UPDATE fno_direction_run SET registration_hash = 'x'"))
    live._CACHE.clear()
    assert client.get("/api/fno/direction/NIFTY?decide=false").json()["model_status"] == "no_edge"


def test_an_accepted_bullish_model_gets_a_trade_and_its_plan_card(client, monkeypatch):
    from finresearch.direction import live

    store(panel())
    st = live.state("NIFTY")
    for hz in st["horizons"]:
        if hz["h"] == 5:
            for ev, p in {"up": 0.95, "up_k": 0.9, "down_k": 0.01}.items():
                hz["events"][ev].update(status="accepted", shown=p, logistic=p, shown_model="logistic")
    monkeypatch.setattr(live, "state", lambda sym, *a, **k: st)
    d = client.get("/api/fno/direction/NIFTY").json()
    dec = d["decision"]
    assert dec["action"] == "TRADE" and dec["chosen"]["view"] == "up" and dec["accepted"] is True
    plan = dec["plan"]
    assert plan["symbol"] == "NIFTY" and plan["legs"] and "(direction model)" in plan["title"]
    assert plan["action"] in ("ENTER", "WAIT", "AVOID")  # the card's own checks still apply


def test_log_window_dedupe_and_automatic_resolution(db):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.direction import live
    from finresearch.signals import ledger

    rows = panel(end=date(2026, 10, 9))
    first = [r for r in rows if r.day <= AS_OF]
    store(first)
    st = live.state("NIFTY")
    evening = datetime(2026, 9, 28, 20, 0, tzinfo=IST)
    res = live.log("NIFTY", evening, st=st, holidays=set())
    assert res["skipped"] is None and len(res["logged"]) >= 12
    again = live.log("NIFTY", datetime(2026, 9, 28, 21, 0, tzinfo=IST), st=st, holidays=set())
    assert sorted(x["id"] for x in again["logged"]) == sorted(x["id"] for x in res["logged"])  # one per day
    late = live.log("NIFTY", datetime(2026, 9, 29, 9, 20, tzinfo=IST), st=st, holidays=set())
    assert late["logged"] == [] and "next session opened" in late["skipped"]
    early = live.log("NIFTY", datetime(2026, 9, 28, 14, 0, tzinfo=IST), st=st, holidays=set())
    assert early["logged"] == [] and "not closed" in early["skipped"]
    with session_scope() as s:
        fs = list(s.scalars(select(Forecast).where(Forecast.source == live.SOURCE)))
        f5 = next(f for f in fs if f.event_kind == "fno_dir_up_h5" and f.inputs["model"] == "implied")
        assert f5.inputs["start_date"] == "2026-09-28" and f5.resolve_on == date(2026, 10, 5)
        assert f5.validation_status == "rule_based" and f5.method == live.METHOD_IMPLIED
        assert {f.validation_status for f in fs if f.inputs["model"] == "logistic"} <= {"shadow"}
    # nothing to resolve yet: the sessions after the start are not stored
    now = datetime(2026, 10, 9, 20, 0, tzinfo=IST)
    stats = __import__("asyncio").run(ledger.resolve_due(None, now))
    assert stats["resolved"] == 0 and stats["waiting"] > 0
    store([r for r in rows if r.day > AS_OF])
    stats = __import__("asyncio").run(ledger.resolve_due(None, now, recheck=True))
    assert stats["resolved"] > 0
    closes = {r.day: round(r.close, 2) for r in rows}
    with session_scope() as s:
        f5 = s.get(Forecast, f5.id)
        r5 = math.log(closes[date(2026, 10, 5)] / closes[AS_OF])
        assert f5.status == "resolved" and f5.outcome == int(r5 > 0)
        assert f5.resolution_value == pytest.approx(r5 * 100)
        f1k = next(f for f in s.scalars(select(Forecast).where(Forecast.event_kind == "fno_dir_up_k_h1")))
        r1 = math.log(closes[date(2026, 9, 29)] / closes[AS_OF])
        assert f1k.outcome == int(r1 > f1k.inputs["threshold_logret"])
        # the 20-session forecasts are still open (only 9 sessions stored after the start)
        assert s.scalar(select(func.count()).select_from(Forecast).where(Forecast.event_kind == "fno_dir_up_h20",
                                                                          Forecast.status == "open")) >= 1  # fmt: skip
        acc = live.live_accuracy(s, "NIFTY")
    assert any(a["event_kind"] == "fno_dir_up_h5" and a["resolved"] >= 1 for a in acc)


def test_resolution_waits_on_a_missing_bhavcopy_day(db):
    import asyncio

    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay, Forecast
    from finresearch.direction import live
    from finresearch.signals import ledger

    rows = panel(end=date(2026, 10, 9))
    store(rows)
    st = live.state("NIFTY")
    st = {**st, "as_of": AS_OF.isoformat(), "spot": round(next(r.close for r in rows if r.day == AS_OF), 2)}
    live.log("NIFTY", datetime(2026, 9, 28, 20, 0, tzinfo=IST), st=st, holidays=set())
    with session_scope() as s:  # 30-Sep was never downloaded: neither a loaded nor a no-file day
        s.execute(FnoBhav.__table__.delete().where(FnoBhav.day == date(2026, 9, 30)))
        s.execute(FnoBhavDay.__table__.delete().where(FnoBhavDay.day == date(2026, 9, 30)))
        f = next(f for f in s.scalars(select(Forecast).where(Forecast.event_kind == "fno_dir_up_h5")))
        with pytest.raises(ledger.NotYet, match="no bhavcopy record for 2026-09-30"):
            asyncio.run(live.resolve_direction(f, None, s))


def test_post_log_route_and_the_monitor_step(client):
    import asyncio

    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.monitor.fno_direction import fno_direction_step

    store(panel())
    r = client.post("/api/fno/direction/NIFTY/log").json()
    assert r["skipped"] is None and r["logged"]
    client.clock["now"] = datetime(2026, 9, 29, 11, 0, tzinfo=IST)  # the next session is open: refused
    assert client.post("/api/fno/direction/NIFTY/log").json()["logged"] == []
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(Forecast))
    # the nightly step: today's bhavcopy loaded → logs once per slot; BANKNIFTY has no history → reported, not fatal
    now = datetime(2026, 9, 28, 20, 0, tzinfo=IST)
    out = asyncio.run(fno_direction_step(now, holidays=set()))
    assert out["fno_direction"]["NIFTY"]["skipped"] is None and "BANKNIFTY" in out["fno_direction"]
    assert asyncio.run(fno_direction_step(now, holidays=set())) in (
        {},
        {"fno_direction": out["fno_direction"]},
    )
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Forecast)) == n  # same day, same methods: deduped
    assert asyncio.run(fno_direction_step(datetime(2026, 9, 28, 18, 0, tzinfo=IST), holidays=set())) == {}

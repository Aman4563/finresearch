"""Time frames: NSE intraday series parsing, candle aggregation (golden values), the intraday archive and API, the
profile's chart defaults and watch windows, and the monitor planner reading them."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from finresearch.adapters.http import IST
from finresearch.adapters.nse_intraday import IntradaySeries, decode_ts, parse_chart
from finresearch.fincalc.candles import Tick, aggregate, session_ticks
from finresearch.monitor.plan import SUBSCRIPTION_TIMES, plan, plan_stock
from finresearch.suggest.profile import ChartDefaults, Preferences, Profile, WatchWindows

FIX = Path(__file__).parent / "fixtures" / "nse"
D = Decimal


def at(h, m, s=0, d=30):
    return datetime(2026, 9, d, h, m, s, tzinfo=IST)


def infy() -> IntradaySeries:
    return parse_chart(json.loads((FIX / "chart_INFYEQN_1D.json").read_text()), "INFY", "equity", "src")


# --------------------------------------------------------------------------- adapter
def test_nse_chart_timestamps_are_ist_wall_clock():
    # probed 30-Sep-2026 at 12:51 IST: the payload's last point is 1790772670000
    assert decode_ts(1790772670000) == at(12, 51, 10)


def test_equity_series_parses_the_quote_page_chart():
    s = infy()
    assert s.prev_close == D("1015.4") and len(s.ticks) == 227 and s.as_of == at(12, 51, 10)
    assert sum(t.phase == "PO" for t in s.ticks) == 9 and s.day == date(2026, 9, 30)
    assert len(session_ticks(s.ticks)) == 218  # pre-open (09:00-09:08) left out


def test_index_series_dedupes_repeated_timestamps():
    s = parse_chart(json.loads((FIX / "index_chart_NIFTY50_1D.json").read_text()), "NIFTY 50", "index", "src")
    assert s.prev_close == D("22716.2") and len(s.ticks) == 234  # 240 rows, 6 repeats
    assert [t.at for t in s.ticks] == sorted({t.at for t in s.ticks})


# --------------------------------------------------------------------------- candles (golden, hand-computed)
def test_five_minute_candles_from_the_fixture():
    c = aggregate(infy().ticks, "5m", live=True)
    assert len(c) == 44  # 09:15 ... 12:50
    first = c[
        0
    ]  # 09:15:00 1004, 09:15:58 995.6, 09:16:59 999, 09:17:59 1000.9, 09:18:59 1000.3, 09:19:59 1004.4
    assert (first.start, first.end) == (at(9, 15), at(9, 20))
    assert (first.open, first.high, first.low, first.close, first.samples) == (
        D("1004"),
        D("1004.4"),
        D("995.6"),
        D("1004.4"),
        6,
    )
    second = c[1]  # 1004.2, 1004.9, 1003.8, 1004.9, 1006.7
    assert (second.open, second.high, second.low, second.close, second.samples) == (
        D("1004.2"),
        D("1006.7"),
        D("1003.8"),
        D("1006.7"),
        5,
    )
    last = c[-1]  # 12:50:59 1008, live 12:51:10 1008.1: still filling
    assert last.start == at(12, 50) and last.close == D("1008.1") and last.partial
    assert not any(x.partial for x in c[:-1])


def test_fifteen_minute_candle_and_no_partial_when_closed():
    c = aggregate(infy().ticks, "15m")
    assert (c[0].open, c[0].high, c[0].low, c[0].close, c[0].samples) == (
        D("1004"),
        D("1011.4"),
        D("995.6"),
        D("1010.1"),
        16,
    )
    assert not c[-1].partial


def test_buckets_align_to_the_open_and_the_close_sample_joins_the_last_bucket():
    ticks = [Tick(at(9, 7, 59), D(90), "PO"), Tick(at(10, 14, 59), D(100)), Tick(at(10, 15, 59), D(101)),
             Tick(at(15, 29, 59), D(110)), Tick(at(15, 30, 20), D(111)), Tick(at(15, 45, 0), D(120)),
             Tick(at(10, 15, 59), D(102))]  # fmt: skip  # repeated timestamp: the last one wins
    h = aggregate(ticks, "1h")
    assert [(x.start, x.end) for x in h] == [
        (at(9, 15), at(10, 15)),
        (at(10, 15), at(11, 15)),
        (at(15, 15), at(15, 30)),
    ]
    assert h[1].open == D(102) and h[2].close == D(111)  # post-close 15:45 sample ignored
    one = aggregate(ticks, "1m")
    assert [x.open for x in one] == [D(100), D(102), D(110)]  # the 15:30:20 close sample joins 15:29
    assert [x.close for x in one] == [D(100), D(102), D(111)]


def test_multi_day_candles_keep_sessions_apart():
    ticks = [Tick(at(15, 29, 59, d=29), D(100)), Tick(at(9, 15, 59, d=30), D(105))]
    c = aggregate(ticks, "30m")
    assert [x.start for x in c] == [at(15, 15, d=29), at(9, 15, d=30)]


def test_unknown_interval_is_refused():
    with pytest.raises(ValueError):
        aggregate(infy().ticks, "2m")


# --------------------------------------------------------------------------- preferences
def test_profiles_saved_before_time_frames_load_with_the_old_behaviour():
    p = Profile.model_validate({"preferences": {"default_landing": "/ipos"}})
    tf, ww = p.preferences.time_frames, p.preferences.watch
    assert (tf.stock.range, tf.stock.interval, tf.stock.type) == ("1Y", "1D", "line")
    assert tf.quote_refresh_s == 30 and tf.fno_refresh_s == 60
    assert ww.subscription_times() == SUBSCRIPTION_TIMES and ww.stock_time() == (16, 30)


def test_chart_defaults_fit_the_interval_to_the_range():
    assert ChartDefaults(range="5D", interval="1m").interval == "15m"
    assert ChartDefaults(range="1Y", interval="5m").interval == "1D"
    assert ChartDefaults(range="1D", interval="1h").interval == "1h"


def test_watch_windows_validate_slots_and_quiet_hours():
    ww = WatchWindows(ipo_check_times=["16:00", "10:00", "10:00"], quiet_start="22:00", quiet_end="07:00")
    assert ww.subscription_times() == ((10, 0), (16, 0), (17, 15))  # sorted, deduped, final check kept
    for bad in (["09:30"], ["17:00"], ["10:15"]):
        with pytest.raises(ValidationError):
            WatchWindows(ipo_check_times=bad)
    with pytest.raises(ValidationError):
        WatchWindows(quiet_start="22:00")
    with pytest.raises(ValidationError):
        Preferences.model_validate({"time_frames": {"quote_refresh_s": 5}})  # faster than the source


# --------------------------------------------------------------------------- planner
def test_planner_defaults_are_unchanged_and_custom_times_are_used():
    args = ("ORIENTCABL", date(2026, 9, 25), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 5))
    hol: set[date] = set()
    assert plan(*args, holidays=hol) == plan(
        *args, holidays=hol, subscription_times=WatchWindows().subscription_times()
    )
    times = WatchWindows(ipo_check_times=["11:00", "14:30"]).subscription_times()
    subs = [s for s in plan(*args, holidays=hol, subscription_times=times) if s.kind == "subscription"]
    assert [s.due_at.strftime("%H:%M") for s in subs if s.due_at.date() == date(2026, 9, 29)] == [
        "11:00",
        "14:30",
        "17:15",
    ]
    assert [s.slot for s in subs if s.params["final"]] == ["ORIENTCABL:subscription:2026-09-29:1715"]
    stock = plan_stock("INFY", date(2026, 9, 28), date(2026, 9, 30), holidays=hol, at=(18, 0))
    assert [s.due_at for s in stock][-1] == datetime(2026, 9, 30, 18, 0, tzinfo=IST)
    assert plan_stock("INFY", date(2026, 9, 30), date(2026, 9, 30), holidays=hol)[0].due_at.hour == 16


def test_sync_slots_follows_the_profiles_watch_windows(env):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob, Watch
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.monitor.scheduler import sync_slots
    from finresearch.suggest.advisor import load_profile, save_profile

    with session_scope() as s:
        s.query(Alert).delete()
        s.query(MonitorJob).delete()
        s.query(Watch).delete()
        co = get_or_create_company(s, "tf-co", "TF Co")
        s.add(Watch(company_id=co.id, kind="stock", nse_symbol="TFCO", meta={}))
    now = datetime(2026, 9, 29, 10, 0, tzinfo=IST)
    sync_slots(now)
    with session_scope() as s:
        due = {
            j.due_at.astimezone(IST).strftime("%H:%M")
            for j in s.query(MonitorJob).filter_by(status="pending")
        }
    assert due == {"16:30"}
    with session_scope() as s:
        p = load_profile(s)
        p.preferences.watch.stock_daily_time = "20:00"
        save_profile(s, p)
    sync_slots(now)
    with session_scope() as s:
        jobs = s.query(MonitorJob).all()
        pending = {j.due_at.astimezone(IST).strftime("%H:%M") for j in jobs if j.status == "pending"}
    assert pending == {"20:00"} and len(jobs) == 4  # the same daily checks, moved (not run twice)
    with session_scope() as s:
        p = load_profile(s)
        p.preferences.watch = WatchWindows()
        save_profile(s, p)


def test_monitor_schedule_route_reads_the_profile_and_the_scheduler(env):
    """The Monitor page's "How monitoring works" box reads its times from here, never hardcoded."""
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.monitor import intraday, iv, jobs
    from finresearch.suggest.advisor import load_profile, save_profile

    with session_scope() as s:
        p = load_profile(s)
        p.preferences.watch = WatchWindows(ipo_check_times=["11:00", "14:30"], stock_daily_time="18:00",
                                           quiet_start="22:00", quiet_end="07:00")  # fmt: skip
        save_profile(s, p)
    try:
        with TestClient(create_app()) as c:
            r = c.get("/api/monitor/schedule").json()
        assert r["ipo"]["check_times"] == ["11:00", "14:30"] and r["ipo"]["final_check"] == "17:15"
        assert r["stock"]["daily_time"] == "18:00" and r["quiet"] == {"start": "22:00", "end": "07:00"}
        assert r["ipo"]["archive_times"] == [f"{h:02d}:{m:02d}" for h, m in jobs.ARCHIVE_TIMES]
        assert r["intraday"]["from"] == "15:45" == f"{intraday.START[0]}:{intraday.START[1]}"
        assert r["iv"]["from"] == "15:50" and r["iv"]["indices"] == list(iv.INDEX_SYMBOLS)
        assert (
            r["ipo"]["listing_times"] == {"open": "10:15", "close": "15:45"}
            and r["ipo"]["allotment_time"] == "19:00"
        )
        assert r["ipo"]["bidding_hours"] == ["10:00", "17:00"] and r["ipo"]["anchor_lockin_days"] == [30, 90]
        assert (
            r["forecasts"] == {"after": "16:30", "every_min": 60}
            and r["running"] is False
            and r["tick_s"] == 60
        )
    finally:
        with session_scope() as s:
            p = load_profile(s)
            p.preferences.watch = WatchWindows()
            save_profile(s, p)


# --------------------------------------------------------------------------- archive + API
def _series(day: int, prices: list[int], kind="equity", sym="INFY", start=(9, 15)) -> IntradaySeries:
    h, m = start
    ticks = [Tick(datetime(2026, 9, day, h, m + i, 59, tzinfo=IST), D(p)) for i, p in enumerate(prices)]
    return IntradaySeries(sym, kind, D(100), ticks, "https://www.nseindia.com/get-quotes/equity?symbol=INFY")


def test_archive_never_shrinks_or_downgrades_a_complete_session(env):
    from finresearch.db import session_scope
    from finresearch.db.models import IntradaySeriesRow
    from finresearch.monitor.intraday import from_json, load_days, store

    with session_scope() as s:
        s.query(IntradaySeriesRow).delete()
        assert store(s, _series(29, [101, 102, 103]), at(16, 0, d=29))  # after the close: complete
        assert not store(s, _series(29, [101, 102]), at(16, 5, d=29))  # fewer samples: kept
    with session_scope() as s:
        assert not store(
            s, _series(29, [101, 102, 103, 104]), at(15, 0, d=29)
        )  # in session: not over complete
        rows = load_days(s, "equity", "INFY", 5)
        assert len(rows) == 1 and rows[0].complete and rows[0].tick_count == 3
        assert [t.price for t in from_json(rows[0].ticks)] == [D(101), D(102), D(103)]
        assert from_json(rows[0].ticks)[0].at == at(9, 15, 59, d=29)


@pytest.fixture
def intraday_app(env, monkeypatch):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import IntradaySeriesRow

    with session_scope() as s:
        s.query(IntradaySeriesRow).delete()
    monkeypatch.setattr("finresearch.api.live._holidays", lambda: {})
    now = {"t": at(9, 26, 10)}
    calls = {"n": 0, "fail": False}

    async def fetch(kind, sym):
        calls["n"] += 1
        if calls["fail"]:
            raise RuntimeError("HTTP 403")
        return _series(30, [101, 103, 99, 102, 104, 105, 106, 107, 108, 109, 110], kind, sym)

    app = create_app(clock=lambda: now["t"])
    app.state.intraday_fetch = fetch
    with TestClient(app) as c:
        yield c, now, calls


def test_intraday_api_candles_freshness_and_cache(intraday_app):
    c, _, calls = intraday_app
    r = c.get("/api/stocks/infy/intraday?interval=5m").json()
    assert r["live"] and r["kind"] == "equity" and r["interval"] == "5m" and not r["has_volume"]
    assert [(x["o"], x["h"], x["l"], x["c"], x["n"]) for x in r["candles"]] == [
        (101, 104, 99, 104, 5), (105, 109, 105, 109, 5), (110, 110, 110, 110, 1)]  # fmt: skip
    assert r["candles"][-1]["partial"] and r["as_of"] == "2026-09-30T09:25:59+05:30"
    assert r["last"] == 110 and r["change"] == 10 and r["change_pct"] == 10 and r["refresh_s"] == 30
    assert r["delay_s"] is not None and "1-minute" in r["source_label"] and "understated" in r["notes"][0]
    c.get("/api/stocks/INFY/intraday?interval=1m")
    assert calls["n"] == 1  # one NSE call per 30 s whatever the interval
    assert c.get("/api/stocks/INFY/intraday?interval=2m").status_code == 422
    idx = c.get("/api/indices/nifty%2050/intraday?interval=15m").json()
    assert idx["kind"] == "index" and idx["symbol"] == "NIFTY 50"
    assert c.get("/api/indices/NIFTY%20SMALLCAP/intraday").status_code == 404
    assert "NIFTY 50" in c.get("/api/indices").json()["indices"]


def test_intraday_api_stitches_archived_sessions_and_says_how_many(intraday_app):
    from finresearch.db import session_scope
    from finresearch.monitor.intraday import store

    c, now, calls = intraday_app
    with session_scope() as s:
        store(s, _series(29, [90, 91, 92]), at(16, 0, d=29))
    r = c.get("/api/stocks/INFY/intraday?interval=15m&days=5").json()
    assert [x["day"] for x in r["sessions"]] == ["2026-09-29", "2026-09-30"]
    assert r["sessions"][0]["complete"] and not r["sessions"][1]["complete"]
    assert r["candles"][0]["t"] == "2026-09-29T09:15:00+05:30" and len(r["candles"]) == 2
    assert any("2 of 5 sessions" in n for n in r["notes"])
    # today's series archived itself (cache-through)
    now["t"], calls["fail"] = at(20, 0), True
    r = c.get("/api/stocks/INFY/intraday?interval=5m&days=2").json()
    assert not r["live"] and [x["day"] for x in r["sessions"]] == ["2026-09-29", "2026-09-30"]
    assert any("did not answer" in n for n in r["notes"]) and r["delay_s"] is None


def test_after_close_archive_completes_watched_and_viewed_symbols(env):
    import asyncio

    from finresearch.db import session_scope
    from finresearch.db.models import Alert, IntradaySeriesRow, MonitorJob, Watch
    from finresearch.monitor.intraday import archive_after_close, reset_tries, store

    reset_tries()
    with session_scope() as s:
        s.query(IntradaySeriesRow).delete()
        s.query(Alert).delete()
        s.query(MonitorJob).delete()
        s.query(Watch).delete()
        store(s, _series(30, [1, 2]), at(11, 0))  # viewed in session: incomplete
    got: list[tuple[str, str]] = []

    async def fetch(kind, sym):
        got.append((kind, sym))
        return _series(30, [1, 2, 3], kind, sym)

    before = asyncio.run(archive_after_close(fetch, at(15, 0), holidays={}))
    assert before["skipped"] and not got
    out = asyncio.run(archive_after_close(fetch, at(15, 50), holidays={}))
    assert (
        ("equity", "INFY") in got
        and ("index", "NIFTY 50") in got
        and set(out["archived"]) >= {"INFY", "NIFTY 50"}
    )
    with session_scope() as s:
        assert all(r.complete for r in s.query(IntradaySeriesRow).all())
    got.clear()
    asyncio.run(archive_after_close(fetch, at(16, 30), holidays={}))
    assert got == []  # everything complete: no more NSE calls
    assert asyncio.run(archive_after_close(fetch, datetime(2026, 10, 3, 16, 0, tzinfo=IST), holidays={}))[
        "skipped"
    ]


def test_after_close_archive_saves_a_bse_only_watch_from_bse(env, monkeypatch):
    """A BSE-only stock watch (exchange "BSE", no NSE symbol) gets its session archived from BSE's own 1-minute series
    under "BSE:<scrip code>", with BSE volume, complete after the close."""
    import asyncio

    from finresearch.adapters import bse_intraday
    from finresearch.adapters.bse_intraday import parse_graph
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, Company, IntradaySeriesRow, MonitorJob, Watch
    from finresearch.monitor import intraday

    intraday.reset_tries()
    data = json.loads(
        (Path(__file__).parent / "fixtures" / "bse" / "stockreachgraph_500209_1D.json").read_text()
    )
    asked: list[str] = []

    async def fake_bse(client, code):  # the BSE adapter behind live_fetch, with the recorded payload
        asked.append(code)
        return parse_graph(data, f"BSE:{code}", code)

    monkeypatch.setattr(bse_intraday, "bse_intraday", fake_bse)
    with session_scope() as s:
        s.query(IntradaySeriesRow).delete()
        s.query(Alert).delete()
        s.query(MonitorJob).delete()
        s.query(Watch).delete()
        co = s.query(Company).filter(Company.slug == "asm-intraday").one_or_none()
        if co is None:
            co = Company(slug="asm-intraday", name="ASM Technologies Ltd")
            s.add(co)
            s.flush()
        s.add(
            Watch(company_id=co.id, kind="stock", exchange="BSE", bse_code="526433", nse_symbol=None, meta={})
        )
    assert ("equity", "BSE:526433") in intraday.targets(date(2026, 9, 30))
    fetched: list[tuple[str, str]] = []

    async def fetch(kind, sym):
        fetched.append((kind, sym))
        if sym.startswith("BSE:"):
            return await intraday.live_fetch(kind, sym)  # the monitor's real routing for BSE keys
        return _series(30, [1, 2, 3], kind, sym)

    out = asyncio.run(intraday.archive_after_close(fetch, at(15, 50), holidays={}))
    assert ("equity", "BSE:526433") in fetched and asked == ["526433"] and "BSE:526433" in out["archived"]
    with session_scope() as s:
        row = (
            s.query(IntradaySeriesRow)
            .filter_by(kind="equity", symbol="BSE:526433", day=date(2026, 9, 30))
            .one()
        )
        assert row.complete and row.tick_count == 217 and row.source.startswith("https://api.bseindia.com/")
        assert row.prev_close == D("1005.5") and intraday.from_json(row.ticks)[0].volume == D(24885)
    fetched.clear()
    asyncio.run(intraday.archive_after_close(fetch, at(16, 30), holidays={}))
    assert ("equity", "BSE:526433") not in fetched  # complete: not fetched again


# --------------------------------------------------------------------------- BSE view ("BSE:<scrip code>")
def test_bse_series_parses_price_and_bse_volume():
    from finresearch.adapters.bse_intraday import parse_graph

    data = json.loads(
        (Path(__file__).parent / "fixtures" / "bse" / "stockreachgraph_500209_1D.json").read_text()
    )
    s = parse_graph(data, "BSE:500209", "500209")
    assert s.symbol == "BSE:500209" and s.prev_close == D("1005.5") and len(s.ticks) == 217
    assert s.ticks[0].at == at(9, 15, 59) and s.as_of == at(12, 51, 59)  # newest-first payload, sorted
    first = aggregate(s.ticks, "5m")[0]  # 09:15:59 996.00/24885 ... 09:19:59 999.80/5218
    assert (first.open, first.high, first.low, first.close) == (
        D("996.00"),
        D("1000.55"),
        D("996.00"),
        D("999.80"),
    )
    assert first.volume == D(24885 + 5012 + 1868 + 1219 + 5218)
    assert sum(c.volume for c in aggregate(s.ticks, "1h")) == D(
        339737
    )  # every minute's volume lands in one bucket


def test_bse_key_is_served_with_volume_and_archived_with_it(intraday_app):
    from finresearch.db import session_scope
    from finresearch.monitor.intraday import from_json, load_days

    c, _, calls = intraday_app
    app = c.app

    async def fetch(kind, sym):
        calls["n"] += 1
        ticks = [Tick(at(9, 15 + i, 59), D(100 + i), "NM", D(10 * (i + 1))) for i in range(7)]
        return IntradaySeries(sym, kind, D(99), ticks, "https://api.bseindia.com/x")

    app.state.intraday_fetch = fetch
    r = c.get("/api/stocks/bse:500209/intraday?interval=5m").json()
    assert r["exchange"] == "BSE" and r["has_volume"] and "BSE volume" in r["source_label"]
    assert [x["v"] for x in r["candles"]] == [150, 130]  # 10+20+30+40+50, 60+70
    assert "BSE's own" in r["notes"][0]
    assert c.get("/api/stocks/BSE:12/intraday").status_code == 422
    with session_scope() as s:
        row = load_days(s, "equity", "BSE:500209", 1)[0]
        assert from_json(row.ticks)[-1].volume == D(70)

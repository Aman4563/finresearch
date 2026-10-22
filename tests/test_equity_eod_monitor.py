"""The nightly full equity bhavcopy job (#373 (a)): its window, holiday skip, one slot a night, a retry while today's
file is not published, and its place in the live monitor and the schedule. NSE mocked with respx; synthetic fixture."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from finresearch.adapters import nse_equity_eod as ee
from finresearch.adapters.http import PoliteClient
from finresearch.monitor import equity_eod as job

FILE = (Path(__file__).parent / "fixtures" / "equity_eod" / "sec_bhavdata_full_synthetic.csv").read_text()
DAY = date(2026, 9, 30)  # a Wednesday
AFTER = datetime(2026, 9, 30, 13, 45, tzinfo=UTC)  # 19:15 IST
BEFORE = datetime(2026, 9, 30, 13, 15, tzinfo=UTC)  # 18:45 IST


async def _no_sleep(_s: float) -> None:
    return None


def test_due_slot_window_and_holidays():
    assert job.due_slot(BEFORE, holidays=set()) is None
    assert job.due_slot(AFTER, holidays=set()) == "equity_eod:2026-09-30"
    assert job.due_slot(AFTER, holidays={DAY}) is None
    assert job.due_slot(datetime(2026, 10, 3, 15, 0, tzinfo=UTC), holidays=set()) is None  # Saturday


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE equity_eod, equity_eod_day, fno_bhav_day, dataset_load"))
            s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'equity_eod:%'"))

    wipe()
    yield env
    wipe()


@respx.mock
async def test_step_retries_until_todays_file_is_published(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, EquityEod

    published: list[bool] = []
    today = respx.get(ee.url_for(DAY)).mock(side_effect=lambda _r: httpx.Response(200, text=FILE.replace(
        "15-Jan-2024", "30-Sep-2026")) if published else httpx.Response(404, text="<html></html>"))  # fmt: skip
    respx.get(url__startswith=ee.ARCHIVES).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0) as http:
        res = await job.equity_eod_step(AFTER, holidays=set(), http=http, keep_raw=False)
        assert res["equity_eod"]["today_pending"] is True
        with session_scope() as s:
            slot = s.get(AlertEvalSlot, "equity_eod:2026-09-30")
            assert slot.result["status"] == "failed" and slot.result["retry_at"]
        assert (
            await job.equity_eod_step(AFTER, holidays=set(), http=http, keep_raw=False) == {}
        )  # not yet due
        published.append(True)
        later = datetime(2026, 9, 30, 14, 20, tzinfo=UTC)
        res = await job.equity_eod_step(later, holidays=set(), http=http, keep_raw=False)
        assert res["equity_eod"]["today_pending"] is False and res["equity_eod"]["loaded"] == 1
    assert today.call_count == 2
    with session_scope() as s:
        assert float(s.get(EquityEod, (DAY, "ALPHACO", "EQ")).close) == 104.00


def test_the_live_monitor_runs_it_and_the_schedule_names_it():
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps
    from finresearch.suggest.profile import WatchWindows

    assert Deps(None, None).equity_eod is False  # tests opt in; Deps.live() turns it on
    assert Deps.live().equity_eod is True
    sj = scheduler.schedule_json(WatchWindows(), running=True)
    assert sj["equity_eod"] == {"after": "19:00", "lookback_days": 4, "requests_per_night": 1}


async def test_tick_spawns_the_job_only_when_due(monkeypatch):
    from finresearch.monitor import scheduler

    calls: list[datetime] = []

    async def fake_step(now, holidays=None, **kw):
        calls.append(now)
        return {}

    monkeypatch.setattr(job, "equity_eod_step", fake_step)
    monkeypatch.setattr(job, "due_slot", lambda now, holidays=None: "equity_eod:x" if now == AFTER else None)
    scheduler._spawn_equity_eod(BEFORE)
    assert "equity_eod" not in scheduler._BACKGROUND or scheduler._BACKGROUND["equity_eod"].done()
    scheduler._spawn_equity_eod(AFTER)
    await scheduler._BACKGROUND["equity_eod"]
    assert calls == [AFTER]


async def test_low_disk_stops_this_download_too(monkeypatch):
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps

    spawned: list[str] = []
    monkeypatch.setattr(scheduler, "_spawn_equity_eod", lambda *a: spawned.append("equity_eod"))
    deps = Deps(None, None, equity_eod=True)
    monkeypatch.setattr(scheduler, "_ingest_allowed", lambda: False)
    scheduler._spawn_downloads(deps, AFTER)
    assert spawned == []
    monkeypatch.setattr(scheduler, "_ingest_allowed", lambda: True)
    scheduler._spawn_downloads(deps, AFTER)
    assert spawned == ["equity_eod"]

"""The nightly index close job (#366): its window, holiday skip, one slot a night, and a retry while today's file is
not published. NSE mocked with respx; synthetic fixture."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from finresearch.adapters import nse_index_eod as ie
from finresearch.adapters.http import PoliteClient
from finresearch.monitor import index_eod as job

CURRENT = (
    Path(__file__).parent / "fixtures" / "index_eod" / "ind_close_all_current_synthetic.csv"
).read_text()
DAY = date(2026, 9, 30)  # a Wednesday
AFTER = datetime(2026, 9, 30, 14, 15, tzinfo=UTC)  # 19:45 IST
BEFORE = datetime(2026, 9, 30, 13, 15, tzinfo=UTC)  # 18:45 IST


async def _no_sleep(_s: float) -> None:
    return None


def test_due_slot_window_and_holidays():
    assert job.due_slot(BEFORE, holidays=set()) is None
    assert job.due_slot(AFTER, holidays=set()) == "index_eod:2026-09-30"
    assert job.due_slot(AFTER, holidays={DAY}) is None
    assert job.due_slot(datetime(2026, 10, 3, 15, 0, tzinfo=UTC), holidays=set()) is None  # Saturday


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE index_eod, index_eod_day, fno_bhav_day"))
            s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'index_eod:%'"))

    wipe()
    yield env
    wipe()


@respx.mock
async def test_step_retries_until_todays_file_is_published(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, IndexEod

    published: list[bool] = []
    today = respx.get(ie.url_for(DAY)).mock(side_effect=lambda _r: httpx.Response(200, text=CURRENT.replace(
        "03-01-2022", "30-09-2026")) if published else httpx.Response(404, text="<html></html>"))  # fmt: skip
    respx.get(url__startswith=ie.ARCHIVES).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0) as http:
        res = await job.index_eod_step(AFTER, holidays=set(), http=http, keep_raw=False)
        assert res["index_eod"]["today_pending"] is True
        with session_scope() as s:
            slot = s.get(AlertEvalSlot, "index_eod:2026-09-30")
            assert slot.result["status"] == "failed" and slot.result["retry_at"]
        assert await job.index_eod_step(AFTER, holidays=set(), http=http, keep_raw=False) == {}  # not yet due
        published.append(True)
        later = datetime(2026, 9, 30, 14, 50, tzinfo=UTC)
        res = await job.index_eod_step(later, holidays=set(), http=http, keep_raw=False)
        assert res["index_eod"]["today_pending"] is False and res["index_eod"]["loaded"] == 1
    assert today.call_count == 2
    with session_scope() as s:
        assert float(s.get(IndexEod, (DAY, "NIFTY")).close) == 17600.50


def test_the_live_monitor_runs_it_and_the_schedule_names_it():
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps
    from finresearch.suggest.profile import WatchWindows

    assert Deps(None, None).index_eod is False  # tests opt in; Deps.live() turns it on
    assert Deps.live().index_eod is True
    sj = scheduler.schedule_json(WatchWindows(), running=True)
    assert sj["index_eod"] == {"after": "19:30", "lookback_days": 4, "requests_per_night": 1}


async def test_tick_spawns_the_job_only_when_due(monkeypatch):
    from finresearch.monitor import scheduler

    calls: list[datetime] = []

    async def fake_step(now, holidays=None, **kw):
        calls.append(now)
        return {}

    monkeypatch.setattr(job, "index_eod_step", fake_step)
    monkeypatch.setattr(job, "due_slot", lambda now, holidays=None: "index_eod:x" if now == AFTER else None)
    scheduler._spawn_index_eod(BEFORE)
    assert "index_eod" not in scheduler._BACKGROUND or scheduler._BACKGROUND["index_eod"].done()
    scheduler._spawn_index_eod(AFTER)
    await scheduler._BACKGROUND["index_eod"]
    assert calls == [AFTER]

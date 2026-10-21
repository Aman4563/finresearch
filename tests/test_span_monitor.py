"""The nightly SPAN job (#370): its window, holiday skip, a retry while today's file is not published, the raw zip
kept for the margin engine, and its place in the live monitor. NSE mocked with respx; synthetic fixtures."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from sqlalchemy import text
from test_span_store import spn_for, volt_for

from finresearch.adapters import nse_span as sp
from finresearch.adapters.http import PoliteClient
from finresearch.monitor import span as job

DAY = date(2026, 10, 16)  # a Friday
AFTER = datetime(2026, 10, 16, 14, 15, tzinfo=UTC)  # 19:45 IST
BEFORE = datetime(2026, 10, 16, 13, 15, tzinfo=UTC)  # 18:45 IST


async def _no_sleep(_s: float) -> None:
    return None


def test_due_slot_window_and_holidays():
    assert job.due_slot(BEFORE, holidays=set()) is None
    assert job.due_slot(AFTER, holidays=set()) == "span:2026-10-16"
    assert job.due_slot(AFTER, holidays={DAY}) is None
    assert job.due_slot(datetime(2026, 10, 17, 15, 0, tzinfo=UTC), holidays=set()) is None  # Saturday


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE span_param, span_day, fo_volt, fno_bhav_day"))
            s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'span:%'"))

    wipe()
    yield env
    wipe()


@respx.mock
async def test_step_retries_until_todays_file_is_published_and_keeps_the_zip(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot
    from finresearch.fnohist import span

    published: list[bool] = []
    respx.get(sp.url_for(DAY)).mock(side_effect=lambda _r: httpx.Response(200, content=spn_for(DAY)) if published
                                    else httpx.Response(404, text="<html/>"))  # fmt: skip
    respx.get(sp.volt_url_for(DAY)).mock(return_value=httpx.Response(200, text=volt_for(DAY)))
    respx.get(url__startswith=sp.ARCHIVES).mock(return_value=httpx.Response(404, text="<html/>"))
    async with PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0) as http:
        res = await job.span_step(AFTER, holidays=set(), http=http)
        assert res["span"]["today_pending"] is True
        with session_scope() as s:
            assert s.get(AlertEvalSlot, "span:2026-10-16").result["status"] == "failed"
        published.append(True)
        res = await job.span_step(datetime(2026, 10, 16, 14, 50, tzinfo=UTC), holidays=set(), http=http)
    assert (
        res["span"]["today_pending"] is False and res["span"]["loaded"] == 1 and res["span"]["volt_rows"] == 2
    )
    assert span.raw_path(DAY).is_file()  # tonight's zip stays for the risk arrays (30 sessions)


def test_the_live_monitor_runs_it_and_the_schedule_names_it():
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps
    from finresearch.suggest.profile import WatchWindows

    assert Deps(None, None).span is False
    assert Deps.live().span is True
    sj = scheduler.schedule_json(WatchWindows(), running=True)
    assert sj["span"] == {
        "after": "19:30",
        "lookback_days": 4,
        "requests_per_night": 2,
        "raw_sessions_kept": 30,
    }


async def test_tick_spawns_the_job_only_when_due(monkeypatch):
    from finresearch.monitor import scheduler

    calls: list[datetime] = []

    async def fake_step(now, holidays=None, **kw):
        calls.append(now)
        return {}

    monkeypatch.setattr(job, "span_step", fake_step)
    monkeypatch.setattr(job, "due_slot", lambda now, holidays=None: "span:x" if now == AFTER else None)
    scheduler._spawn_span(BEFORE)
    assert "span" not in scheduler._BACKGROUND or scheduler._BACKGROUND["span"].done()
    scheduler._spawn_span(AFTER)
    await scheduler._BACKGROUND["span"]
    assert calls == [AFTER]

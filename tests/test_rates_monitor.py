"""#366: the nightly FBIL rates job (monitor.rates): its window, one slot a night, the recent-window fetch, and a
missing data-rights decision recorded as skipped (no request, no retry storm). FBIL mocked with respx."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from sqlalchemy import text

from finresearch.adapters import fbil
from finresearch.adapters.http import PoliteClient
from finresearch.monitor import rates as job

AFTER = datetime(2026, 9, 30, 15, 15, tzinfo=UTC)  # Wed 20:45 IST
BEFORE = datetime(2026, 9, 30, 14, 15, tzinfo=UTC)  # 19:45 IST


async def _no_sleep(_s: float) -> None:
    return None


def test_due_slot_weekday_evenings_only():
    assert job.due_slot(BEFORE) is None
    assert job.due_slot(AFTER) == "rates:2026-09-30"
    assert job.due_slot(datetime(2026, 10, 3, 15, 15, tzinfo=UTC)) is None  # Saturday


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE rate_curve, rate_curve_load, dataset_load"))
        s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'rates:%'"))
    yield env


async def test_no_decision_is_skipped_without_requests(clean):
    with respx.mock(assert_all_called=False) as mock:
        res = await job.rates_step(AFTER)
    assert "skipped" in res["rates"] and not mock.calls
    assert await job.rates_step(AFTER) == {}  # the night's slot is taken: not retried


async def test_recent_window_fetched_once_a_night(clean, monkeypatch):
    from finresearch import data_rights

    monkeypatch.setitem(data_rights.DECISIONS, "fbil_rates", data_rights.Decision(
        "fbil_rates", "accept-personal", "2026-10-22", "test", "test"))  # fmt: skip
    with respx.mock(assert_all_called=False) as mock:
        fix = mock.get(url__regex=r".*/(tbill|ovnmibor|termmibor)/fetchfiltered").mock(
            return_value=httpx.Response(200, json=[])
        )
        listing = mock.get(f"{fbil.BASE}/gsec/fetchfiltered").mock(return_value=httpx.Response(200, json=[]))
        http = PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)
        res = await job.rates_step(AFTER, http=http)
        assert res["rates"]["failed"] == {}
        starts = sorted({c.request.url.params["fromDate"] for c in fix.calls})
        assert starts == [
            (date(2026, 9, 30) - job.LOOKBACK).isoformat(),
            "2026-09-01",
        ]  # 45 days: two windows
        assert len(fix.calls) == 6  # three series
        assert listing.called
        assert await job.rates_step(AFTER, http=http) == {}


def test_the_live_monitor_runs_it_and_the_schedule_names_it():
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps
    from finresearch.suggest.profile import WatchWindows

    assert Deps(None, None).rates is False  # tests opt in; Deps.live() turns it on
    assert Deps.live().rates is True
    sj = scheduler.schedule_json(WatchWindows(), running=True)
    assert sj["rates"] == {"after": "20:30", "lookback_days": 45, "trading_days": False}


async def test_tick_spawns_the_job_only_when_due(monkeypatch):
    import asyncio

    from finresearch.monitor import rates as mr
    from finresearch.monitor import scheduler

    calls: list[datetime] = []

    async def fake_step(now, **kw):
        calls.append(now)
        return {}

    monkeypatch.setattr(mr, "rates_step", fake_step)
    monkeypatch.setattr(scheduler, "_BACKGROUND", {})
    scheduler._spawn_rates(BEFORE)
    scheduler._spawn_rates(AFTER)
    await asyncio.gather(*scheduler._BACKGROUND.values())
    assert calls == [AFTER]

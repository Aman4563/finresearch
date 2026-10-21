"""The monitor's data-quality job (#368): its two windows (after the evening SLAs, and before the 08:30 brief), the
holiday skip, one claim per slot, a retry after a crash, its Deps flag and schedule entry, and the disk gate that
stops the nightly downloads. Test database only."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from finresearch.monitor import dq as job

DAY = date(2026, 9, 30)  # a Wednesday
NIGHT = datetime(2026, 9, 30, 15, 30, tzinfo=UTC)  # 21:00 IST
EARLY_EVENING = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)  # 19:30 IST
MORNING = datetime(2026, 9, 30, 2, 35, tzinfo=UTC)  # 08:05 IST
FREE = lambda _p: 50 * 2**30  # noqa: E731


def test_due_slots():
    assert job.due_slot(EARLY_EVENING, holidays=set()) is None
    assert job.due_slot(NIGHT, holidays=set()) == "dq:evening:2026-09-30"
    assert job.due_slot(MORNING, holidays=set()) == "dq:morning:2026-09-30"
    assert (
        job.due_slot(datetime(2026, 9, 30, 3, 5, tzinfo=UTC), holidays=set()) is None
    )  # 08:35: after the brief
    assert job.due_slot(NIGHT, holidays={DAY}) is None
    assert job.due_slot(datetime(2026, 10, 3, 15, 30, tzinfo=UTC), holidays=set()) is None  # Saturday


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE dq_check, dataset_status"))
            s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'dq:%'"))

    wipe()
    yield env
    wipe()


def test_step_claims_once_and_retries_after_a_crash(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot
    from finresearch.dq import run

    res = job.dq_step(NIGHT, holidays=set(), free_bytes=FREE)
    assert res["dq"]["checks"] > 0 and res["dq"]["status"]["disk"] == "ok"
    assert job.dq_step(NIGHT, holidays=set(), free_bytes=FREE) == {}  # claimed: not run twice
    with session_scope() as s:
        assert s.get(AlertEvalSlot, "dq:evening:2026-09-30").result["status"] == "done"

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(run, "run_all", boom)
    assert job.dq_step(MORNING, holidays=set(), free_bytes=FREE) == {"dq": "failed"}
    with session_scope() as s:
        r = s.get(AlertEvalSlot, "dq:morning:2026-09-30").result
        assert r["status"] == "failed" and r["retry_at"]


def test_deps_flag_and_schedule():
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps
    from finresearch.suggest.profile import WatchWindows

    assert Deps(None, None).dq is False
    assert Deps.live().dq is True
    assert scheduler.schedule_json(WatchWindows(), running=True)["dq"] == {"evening": "21:00", "morning": "08:00",
                                                                           "min_free_gib": 10}  # fmt: skip


async def test_low_disk_stops_the_nightly_downloads(monkeypatch):
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps

    spawned: list[str] = []
    for name in ("fno_bhav", "index_eod", "span", "quotes"):
        monkeypatch.setattr(scheduler, f"_spawn_{name}", lambda *a, _n=name: spawned.append(_n))
    deps = Deps(None, None, fno_bhav=True, index_eod=True, span=True, quotes=True)
    monkeypatch.setattr(scheduler, "_ingest_allowed", lambda: False)
    scheduler._spawn_downloads(deps, NIGHT)
    assert spawned == []
    monkeypatch.setattr(scheduler, "_ingest_allowed", lambda: True)
    scheduler._spawn_downloads(deps, NIGHT)
    assert spawned == ["fno_bhav", "quotes", "index_eod", "span"]

"""The monitor's monthly re-validation pass (#379): one claim per calendar month, after the evening jobs and outside
market hours, run as a low-priority worker; a failed worker is retried, a finished month is never re-run; the Deps
flag is off by default and on in Deps.live(). Test database only; the worker is faked (no replay here)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from finresearch.monitor import revalidate as job

NIGHT = datetime(2026, 11, 1, 17, 0, tzinfo=UTC)  # 22:30 IST, 1-Nov-2026 (a Sunday)
EVENING = datetime(2026, 11, 1, 16, 0, tzinfo=UTC)  # 21:30 IST


def test_due_slot_is_one_per_month_from_its_time():
    assert job.due_slot(EVENING) is None
    assert job.due_slot(NIGHT) == "revalidate:2026-11"
    assert (
        job.due_slot(NIGHT + timedelta(days=9)) == "revalidate:2026-11"
    )  # a missed night: the same month's slot
    assert job.due_slot(datetime(2026, 12, 1, 17, 0, tzinfo=UTC)) == "revalidate:2026-12"


def test_due_slot_is_never_inside_market_hours(monkeypatch):
    monkeypatch.setattr(job, "RUN_AFTER", datetime(2026, 1, 1, 10, 0).time())  # a misconfigured time
    assert job.due_slot(datetime(2026, 11, 2, 5, 0, tzinfo=UTC)) is None  # 10:30 IST on a Monday: in session


def test_the_worker_is_the_cli_under_the_batch_budget():
    argv = job.worker_argv("revalidate:2026-11")
    assert argv[-4:] == ["models", "revalidate", "--slot", "revalidate:2026-11"] and "finresearch.cli" in argv


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE alert_eval_slot"))


def test_a_month_is_claimed_once_and_a_failed_worker_is_retried(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot

    calls: list[str] = []
    assert job.revalidate_step(NIGHT, runner=lambda slot: calls.append(slot) or 2) == {"revalidate": "failed"}
    with session_scope() as s:
        r = s.get(AlertEvalSlot, "revalidate:2026-11").result
        assert r["status"] == "failed" and r["returncode"] == 2 and r["retry_at"]
    assert job.revalidate_step(NIGHT + timedelta(minutes=5), runner=lambda slot: 0) == {}  # before retry_at
    later = NIGHT + timedelta(hours=1)
    assert job.revalidate_step(later, runner=lambda slot: calls.append(slot) or 0) == {"revalidate": "done"}
    assert job.revalidate_step(later + timedelta(days=1), runner=lambda slot: calls.append(slot) or 0) == {}
    assert calls == ["revalidate:2026-11", "revalidate:2026-11"]


def test_a_deferred_worker_is_a_failed_attempt_not_done(clean):
    assert job.revalidate_step(NIGHT, runner=lambda slot: None) == {"revalidate": "failed"}


def test_deps_flag_and_schedule():
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps
    from finresearch.suggest.profile import WatchWindows

    assert Deps(None, None).revalidate is False and Deps.live().revalidate is True
    sch = scheduler.schedule_json(WatchWindows(), running=True)["revalidate"]
    assert sch == {"after": "22:30", "every": "month", "priority": "background"}

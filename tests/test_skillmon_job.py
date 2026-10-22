"""The skill monitor's nightly pass and its brief section (#378): one claim per day, a retry after a crash, the Deps
flag and schedule entry; in the brief an alarm is a red line and counts in the headline, while a monitor that has not
run, has gone stale or has too few outcomes is said so, never shown as ok. Test database only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from finresearch.monitor import skill as job

NIGHT = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)  # 21:30 IST, Wednesday
EVENING = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)  # 20:30 IST
BRIEF_AT = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)  # 08:30 IST next day


def test_due_slots() -> None:
    assert job.due_slot(EVENING) is None
    assert job.due_slot(NIGHT) == "skill:2026-09-30"
    assert (
        job.due_slot(datetime(2026, 10, 3, 17, 0, tzinfo=UTC)) == "skill:2026-10-03"
    )  # Saturday: database only


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE forecast"))
            s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'skill:%'"))

    wipe()
    yield env
    wipe()


def _forecast(i: int, method: str, p: float, o: int, day: date):
    from finresearch.db.models import Forecast

    return Forecast(asset="fno", instrument="NIFTY", source="direction", event_kind="up_h5", event="e", horizon="5",
                    resolve_on=day + timedelta(days=7), probability=p, action="INFORMATIONAL", method=method,
                    validation_status="shadow", status="resolved", outcome=o,
                    resolved_at=datetime.combine(day + timedelta(days=8), datetime.min.time(), UTC),
                    inputs={"start_date": day.isoformat(), "h": 5, "event": "up"}, dedupe_key=f"t{i}",
                    created_at=datetime.combine(day, datetime.min.time(), UTC))  # fmt: skip


def test_step_stores_the_report_claims_once_and_retries_after_a_crash(clean, monkeypatch) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot
    from finresearch.direction.live import METHOD_IMPLIED, METHOD_LOGIT
    from finresearch.skillmon import monitor

    with session_scope() as s:
        for i in range(3):
            d = date(2026, 6, 1) + timedelta(days=10 * i)
            s.add_all(
                [_forecast(2 * i, METHOD_LOGIT, 0.7, 1, d), _forecast(2 * i + 1, METHOD_IMPLIED, 0.5, 1, d)]
            )
    res = job.skill_step(NIGHT)
    assert res["skill"]["counts"]["insufficient data"] == 2 and res["skill"]["alarms"] == 0
    assert job.skill_step(NIGHT) == {}  # claimed: not run twice
    with session_scope() as s:
        r = s.get(AlertEvalSlot, "skill:2026-09-30").result
        assert r["status"] == "done" and len(r["cells"]) == 2
        lg = next(c for c in r["cells"] if c["cell"]["method"] == METHOD_LOGIT)
        assert lg["skill"]["n"] == 3 and lg["skill"]["promotion"] == "insufficient data"

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(monitor, "report", boom)
    later = NIGHT + timedelta(days=1)
    assert job.skill_step(later) == {"skill": "failed"}
    with session_scope() as s:
        r = s.get(AlertEvalSlot, "skill:2026-10-01").result
        assert r["status"] == "failed" and r["retry_at"]


def test_deps_flag_and_schedule() -> None:
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps
    from finresearch.suggest.profile import WatchWindows

    assert Deps(None, None).skill is False
    assert Deps.live().skill is True
    assert scheduler.schedule_json(WatchWindows(), running=True)["skill"] == {
        "after": "21:30",
        "trading_days": False,
    }


def _store(day: str, result: dict) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot

    with session_scope() as s:
        s.add(AlertEvalSlot(slot=f"skill:{day}", result={"status": "done", "attempts": 1, **result}))


def _cell(status: str, alarms: list | None = None, n: int = 12) -> dict:
    return {
        "label": f"model {status}",
        "status": status,
        "independent": n,
        "trigger": 100,
        "alarms": alarms or [],
    }


def test_brief_says_the_monitor_has_not_run(clean) -> None:
    from finresearch.db import session_scope

    with session_scope() as s:
        sec = job.brief_section(s, BRIEF_AT)
    assert sec["status"] == "not run" and sec["alarms"] == []
    assert any("has not run" in x["text"] for x in sec["lines"])


def test_brief_lists_alarms_and_insufficient_cells(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.monitor.digest import brief_text, build_brief

    alarm = {"check": "cusum", "text": "outcomes came more often than forecast"}
    _store("2026-09-30", {"generated_at": NIGHT.isoformat(), "alarms": 1,
                          "cells": [_cell("alarm", [alarm], 140), _cell("insufficient data"), _cell("unknown", n=120)]})  # fmt: skip
    with session_scope() as s:
        b = build_brief(s, BRIEF_AT)
    sec = b["skill"]
    assert sec["status"] == "current" and len(sec["alarms"]) == 1
    texts = [h["text"] for h in b["health"]]
    assert any(h["level"] == "warn" and "outcomes came more often" in h["text"] for h in b["health"])
    assert any("insufficient data" in t and "12 of 100" in t for t in texts)
    assert any("could not be checked" in t for t in texts)
    assert "skill alarm" in b["headline"] and not b["headline"].startswith("Nothing needs a decision")
    assert "• Skill alarm: model alarm" in brief_text(b)


def test_a_stale_report_is_flagged(clean) -> None:
    from finresearch.db import session_scope

    old = NIGHT - timedelta(days=5)
    _store("2026-09-25", {"generated_at": old.isoformat(), "alarms": 0, "cells": [_cell("ok", n=150)]})
    with session_scope() as s:
        sec = job.brief_section(s, BRIEF_AT)
    assert sec["status"] == "stale" and any("2026-09-25" in x["text"] for x in sec["lines"])


def test_cli_skill_prints_every_cell_read_only(clean, tmp_path) -> None:
    import json

    from typer.testing import CliRunner

    from finresearch.cli import app
    from finresearch.db import session_scope
    from finresearch.direction.live import METHOD_IMPLIED, METHOD_LOGIT

    with session_scope() as s:
        s.add_all([_forecast(0, METHOD_LOGIT, 0.7, 1, date(2026, 6, 1)),
                   _forecast(1, METHOD_IMPLIED, 0.5, 1, date(2026, 6, 1))])  # fmt: skip
    out = tmp_path / "skill.json"
    r = CliRunner().invoke(app, ["forecasts", "skill", "--json", str(out)])
    assert r.exit_code == 0, r.output
    assert "insufficient data" in r.output and "1 of 100" in r.output
    assert json.loads(out.read_text())["counts"]["insufficient data"] == 2
    with session_scope() as s:  # read only: no slot claimed
        assert s.execute(text("SELECT count(*) FROM alert_eval_slot WHERE slot LIKE 'skill:%'")).scalar() == 0


def test_cli_skill_refuses_a_changed_spec(env, monkeypatch) -> None:
    from typer.testing import CliRunner

    from finresearch.cli import app
    from finresearch.skillmon import prereg

    monkeypatch.setattr(prereg, "HASH", "0" * 64)
    r = CliRunner().invoke(app, ["forecasts", "skill"])
    assert r.exit_code != 0 and "refused" in r.output

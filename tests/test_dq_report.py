"""Data health in the brief and GET /api/data-health (#368, acceptance 3): a failed critical check shows a red line in
the brief and marks the dependent pages unverified; no check run is unknown, never ok. Test database only."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

NOW = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)  # Thu 08:30 IST
DAY = date(2026, 9, 30)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE dq_check, dataset_status"))
    yield env
    with session_scope() as s:
        s.execute(text("TRUNCATE dq_check, dataset_status"))


def seed_fail(s):
    from finresearch.db.models import DatasetStatus, DqCheck

    s.add(DatasetStatus(dataset="fno_bhav", day=DAY, last_loaded=date(2026, 9, 29), lag_sessions=1, status="fail",
                        failed=["freshness"], checked_at=NOW))  # fmt: skip
    s.add(DqCheck(dataset="fno_bhav", day=DAY, check="freshness", status="fail", critical=True, value=1, threshold=0,
                  note="last loaded 2026-09-29, expected 2026-09-30", run_at=NOW))  # fmt: skip
    s.add(DatasetStatus(dataset="index_eod", day=DAY, last_loaded=DAY, lag_sessions=0, status="ok", failed=[],
                        checked_at=NOW))  # fmt: skip


def test_report_without_any_run_is_unknown(clean):
    from finresearch.db import session_scope
    from finresearch.dq import report

    with session_scope() as s:
        r = report.data_health(s)
    assert r["status"] == "unknown" and r["datasets"] == []
    assert all(d["status"] == "unknown" for d in r["dependents"])


def test_failed_check_reaches_the_report_and_the_brief(clean):
    from finresearch.db import session_scope
    from finresearch.dq import report
    from finresearch.monitor.digest import brief_text, build_brief

    with session_scope() as s:
        seed_fail(s)
    with session_scope() as s:
        r = report.data_health(s)
        b = build_brief(s, NOW)
    assert r["status"] == "fail"
    bhav = next(d for d in r["datasets"] if d["dataset"] == "fno_bhav")
    assert bhav["checks"][0]["note"] == "last loaded 2026-09-29, expected 2026-09-30"
    direction = next(d for d in r["dependents"] if d["page"] == "F&O direction model")
    assert direction["status"] == "unverified"
    assert b["data_health"]["status"] == "fail"
    red = [h for h in b["health"] if h["level"] == "warn" and "F&O bhavcopy" in h["text"]]
    assert red and "unverified" in red[0]["text"]
    assert "Data health" in brief_text(b)
    assert not b["headline"].startswith("Nothing needs a decision")


def test_api_data_health(clean):
    from fastapi.testclient import TestClient

    from finresearch.api.app import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        seed_fail(s)
    c = TestClient(create_app())
    body = c.get("/api/data-health").json()
    assert body["status"] == "fail" and {d["dataset"] for d in body["datasets"]} == {"fno_bhav", "index_eod"}
    assert (
        c.get("/api/data-health/checks", params={"dataset": "fno_bhav"}).json()["checks"][0]["check"]
        == "freshness"
    )


def test_push_text_names_the_failed_dataset(clean):
    from finresearch.db import session_scope
    from finresearch.monitor.digest import brief_text, build_brief

    with session_scope() as s:
        seed_fail(s)
    with session_scope() as s:
        text_ = brief_text(build_brief(s, NOW))
    assert "• Data check failed: F&O bhavcopy (freshness)" in text_

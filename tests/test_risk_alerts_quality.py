"""P4.5 (#398): alert quality. One tap records "acted" or "ignored" on an alert (a POST; GETs write nothing);
precision = actioned / fired per rule, and an informational rule with precision below 20 % after at least 10 firings
is suggested for muting, never a limit, margin, ES or gap-through-stop rule. The brief carries the standing risk
alerts, what cannot be checked and the mute suggestions. Test database only; synthetic alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

NOW = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE alert, alert_delivery, alert_rule_state, alert_eval_slot CASCADE"))

    wipe()
    yield env
    wipe()


def _fire(rule: str, n: int, acted: int, status: str = "fired") -> list[int]:
    from finresearch.db import session_scope
    from finresearch.db.models import Alert

    ids = []
    with session_scope() as s:
        for i in range(n):
            a = Alert(kind="risk_alert", level="warn", message=f"synthetic {rule} {i}",
                      data={"rule": rule, "status": status, "instrument": "NIFTY",
                            **({"feedback": "acted"} if i < acted else {})})  # fmt: skip
            s.add(a)
            s.flush()
            ids.append(a.id)
    return ids


def test_precision_per_rule_and_mute_suggestions(clean) -> None:
    from finresearch.alerts import quality
    from finresearch.db import session_scope

    _fire("iv_shock", 10, 1)  # 10 % after 10 firings: suggest muting
    _fire("gap", 9, 0)  # too few firings to judge
    _fire("gap_stop", 30, 0)  # never suggested
    _fire("touch", 3, 0, status="unknown")  # a can't-check alert is not a firing
    with session_scope() as s:
        rows = {r["rule"]: r for r in quality.by_rule(s)}
    assert rows["iv_shock"]["fired"] == 10 and rows["iv_shock"]["precision"] == pytest.approx(0.1)
    assert rows["iv_shock"]["suggest_mute"] is True
    assert rows["gap"]["suggest_mute"] is False and rows["gap_stop"]["suggest_mute"] is False
    assert rows["gap_stop"]["never_mute"] is True
    assert rows.get("touch", {}).get("fired", 0) == 0


def test_feedback_is_a_post_and_quality_is_a_read_only_get(clean) -> None:
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import Alert

    (aid,) = _fire("iv_shock", 1, 0)
    with TestClient(create_app()) as c:
        r = c.post(f"/api/alerts/{aid}/feedback", json={"outcome": "acted"}, headers=ORIGIN)
        assert r.status_code == 200 and r.json()["data"]["feedback"] == "acted" and r.json()["read_at"]
        assert (
            c.post(f"/api/alerts/{aid}/feedback", json={"outcome": "maybe"}, headers=ORIGIN).status_code
            == 422
        )
        assert (
            c.post("/api/alerts/999999/feedback", json={"outcome": "acted"}, headers=ORIGIN).status_code
            == 404
        )
        with session_scope() as s:
            before = s.get(Alert, aid).data
        q = c.get("/api/alerts/quality").json()
        assert q["rules"][0]["rule"] == "iv_shock" and q["rules"][0]["acted"] == 1
        assert "20%" in q["rule_text"]
        with session_scope() as s:
            assert s.get(Alert, aid).data == before


def test_the_brief_shows_standing_risk_alerts_and_what_cannot_be_checked(clean) -> None:
    from finresearch.alerts import risk
    from finresearch.db import session_scope
    from finresearch.monitor import risk_alerts

    with session_scope() as s:
        risk.apply(
            s, risk.RULES["limit_shock"], "BOOK", risk.Reading(0.9, "synthetic series", "worst loss"), NOW
        )
        risk.apply(
            s, risk.RULES["es"], "BOOK", risk.Reading(None, "no ES series", "not computed yet (P3.4)"), NOW
        )
        sec = risk_alerts.brief_section(s, NOW + timedelta(hours=11))
    text_ = " ".join(x["text"] for x in sec["lines"])
    assert sec["standing"] == 1 and "Common-shock budget used" in text_ and "P3.4" in text_
    assert "have not run" in text_  # no finished pass yet: unchecked, not ok
    with session_scope() as s:
        assert not s.scalars(select(risk.Alert).where(risk.Alert.kind == "brief")).first()


def test_the_brief_includes_the_risk_section(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.monitor import digest

    with session_scope() as s:
        b = digest.build_brief(s, NOW)
    assert "risk" in b and any("risk alert" in x["text"].lower() for x in b["health"])

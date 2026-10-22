"""P4.5 (#398): the risk alerts' state machine and routing (alerts.risk). A limit alert fires at U >= 0.8 and clears
only below 0.7; an unknown input raises one "can't check" alert (never silence) and is not repeated while it stays
unknown; urgent alerts are pushed (priority urgent), the rest only recorded for the brief; the monitor's forwarder
does not push risk alerts a second time. Test database only; synthetic values."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

NOW = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)  # 21:30 IST
TOPIC = "-".join(("synthetic", "topic", "p45", "abcd"))


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE alert, alert_delivery, alert_rule_state, notification_setting CASCADE"))

    wipe()
    yield env
    wipe()


def _ntfy_on():
    from finresearch.db import session_scope
    from finresearch.monitor import notify

    with session_scope() as s:
        notify.update(s, {"ntfy": {"enabled": True, "topic": TOPIC}})


def _apply(rule: str, inst: str, value: float | None, at: datetime = NOW, why: str = "") -> str:
    from finresearch.alerts import risk
    from finresearch.db import session_scope

    with session_scope() as s:
        return risk.apply(
            s, risk.RULES[rule], inst, risk.Reading(value, "synthetic series", why or "detail"), at
        )


def _alerts():
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, AlertDelivery

    with session_scope() as s:
        a = [(x.data["rule"], x.data["status"], x.level) for x in s.scalars(select(Alert).order_by(Alert.id))]
        d = [(x.channel, x.priority) for x in s.scalars(select(AlertDelivery).order_by(AlertDelivery.id))]
    return a, d


def test_limit_fires_at_0_8_stays_fired_down_to_0_7_and_refires_after_clearing(clean) -> None:
    _ntfy_on()
    seq = [0.79, 0.81, 0.75, 0.70, 0.69, 0.80]
    got = [_apply("limit_shock", "BOOK", u, NOW + timedelta(days=i)) for i, u in enumerate(seq)]
    assert got == ["clear", "fired", "still_fired", "still_fired", "clear", "fired"]
    alerts, deliveries = _alerts()
    assert alerts == [("limit_shock", "fired", "action")] * 2
    assert deliveries == [("ntfy", "urgent")] * 2  # urgent: pushed, through quiet hours


def test_an_unknown_input_raises_one_cant_check_alert_never_silence(clean) -> None:
    _ntfy_on()
    assert _apply("iv_shock", "NIFTY", None, why="IV30 shock: 100 sessions stored") == "unknown"
    assert _apply("iv_shock", "NIFTY", None, NOW + timedelta(days=1)) == "still_unknown"
    assert _apply("iv_shock", "NIFTY", 1.0, NOW + timedelta(days=2)) == "clear"
    assert (
        _apply("iv_shock", "NIFTY", None, NOW + timedelta(days=3)) == "unknown"
    )  # unknown again: said again
    alerts, deliveries = _alerts()
    assert alerts == [("iv_shock", "unknown", "warn")] * 2
    assert deliveries == []  # a warn-tier rule goes to the brief, not the phone


def test_an_unknown_limit_input_is_pushed_and_names_the_series(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import Alert

    _ntfy_on()
    assert _apply("es", "BOOK", None, why="no ES engine yet") == "unknown"
    with session_scope() as s:
        a = s.scalars(select(Alert)).one()
        assert (
            "can't check" in a.message and "synthetic series" in a.message and "no ES engine yet" in a.message
        )
        assert a.data["series"] == "synthetic series" and a.data["tier"] == "urgent"
    assert _alerts()[1] == [("ntfy", "high")]  # pushed, but held by quiet hours (only a breach is urgent)


def test_a_warn_tier_firing_is_recorded_for_the_brief_and_not_forwarded(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.monitor import notify

    _ntfy_on()
    with session_scope() as s:
        notify.update(s, {"general": {"forward_level": "warn", "forward_channels": ["ntfy"]}})
    assert _apply("iv_shock", "NIFTY", 3.4) == "fired"
    with session_scope() as s:
        assert notify.forward_monitor_alerts(s, NOW) == 0  # risk alerts route themselves
    alerts, deliveries = _alerts()
    assert alerts == [("iv_shock", "fired", "warn")] and deliveries == []


def test_editing_a_rule_definition_starts_its_state_afresh(clean, monkeypatch) -> None:
    import dataclasses

    from finresearch.alerts import risk

    assert _apply("iv_shock", "NIFTY", 3.4) == "fired"
    monkeypatch.setitem(risk.RULES, "iv_shock", dataclasses.replace(risk.RULES["iv_shock"], fire=4.0))
    assert _apply("iv_shock", "NIFTY", 4.5, NOW + timedelta(days=1)) == "fired"  # not "still_fired"

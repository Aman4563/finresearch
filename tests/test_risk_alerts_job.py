"""P4.5 (#398): the nightly risk-alert pass (monitor.risk_alerts): due on trading days from 20:45 IST, it waits for
tonight's bhavcopy and marks, and at the 23:30 deadline runs anyway so missing inputs raise "can't check" alerts
instead of silence; one claim per day, a crash is retried; a Deps flag and a schedule entry. Test database only."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from finresearch.monitor import risk_alerts as job

DAY = date(2026, 10, 21)  # Wednesday
EARLY = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 19:30 IST
NIGHT = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)  # 21:00 IST
LATE = datetime(2026, 10, 21, 18, 15, tzinfo=UTC)  # 23:45 IST


def test_due_slots_on_trading_days_after_the_evening_files() -> None:
    assert job.due_slot(EARLY, holidays=set()) is None
    assert job.due_slot(NIGHT, holidays=set()) == "risk_alerts:2026-10-21"
    assert job.due_slot(NIGHT, holidays={DAY}) is None
    assert job.due_slot(datetime(2026, 10, 24, 16, 0, tzinfo=UTC), holidays=set()) is None  # Saturday


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE alert, alert_delivery, alert_rule_state, alert_eval_slot, fno_bhav_day, "
                           "fno_mark, fno_iv_eod, fno_bhav CASCADE"))  # fmt: skip

    wipe()
    yield env
    wipe()


def _ready() -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, FnoBhavDay

    with session_scope() as s:
        s.add(FnoBhavDay(day=DAY, status="loaded"))
        s.add(AlertEvalSlot(slot=f"fno_marks:{DAY}", result={"status": "done", "real_note": None}))


def test_it_waits_for_tonights_inputs_until_the_deadline_then_says_what_it_cannot_check(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import Alert

    assert job.risk_step(NIGHT, holidays=set()) == {}  # the bhavcopy is not loaded yet: not claimed
    res = job.risk_step(LATE, holidays=set())
    assert res["risk_alerts"]["unknown"] > 0
    with session_scope() as s:
        got = {(a.data["rule"], a.data["instrument"]) for a in s.scalars(select(Alert))}
    assert ("iv_shock", "NIFTY") in got and ("gap", "BANKNIFTY") in got and ("touch", "BOOK") in got
    assert job.risk_step(LATE, holidays=set()) == {}  # claimed: not run twice


def test_with_inputs_ready_it_runs_at_once_and_an_empty_book_is_within_limits(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertRuleState
    from finresearch.suggest import advisor

    _ready()
    with session_scope() as s:
        p = advisor.load_profile(s)
        p.fno_capital_inr = Decimal(1_000_000)
        advisor.save_profile(s, p)
    res = job.risk_step(NIGHT, holidays=set())
    assert "risk_alerts" in res
    with session_scope() as s:
        st = s.scalars(select(AlertRuleState).where(AlertRuleState.rule_id == "risk:limit_shock")).one()
        assert st.status == "clear" and float(st.value) == 0.0
        assert not s.scalars(select(AlertRuleState).where(AlertRuleState.rule_id == "risk:es")).first()


def test_a_crash_is_recorded_and_retried(clean, monkeypatch) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(job, "evaluate", boom)
    assert job.risk_step(LATE, holidays=set()) == {"risk_alerts": "failed"}
    with session_scope() as s:
        r = s.get(AlertEvalSlot, "risk_alerts:2026-10-21").result
    assert r["status"] == "failed" and "synthetic failure" in r["error"]


def test_deps_flag_and_schedule_entry() -> None:
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps
    from finresearch.suggest.profile import WatchWindows

    assert Deps(None, None).risk_alerts is False
    assert Deps.live().risk_alerts is True
    assert scheduler.schedule_json(WatchWindows(), running=True)["risk_alerts"] == {
        "after": "20:45", "deadline": "23:30", "trading_days": True}  # fmt: skip

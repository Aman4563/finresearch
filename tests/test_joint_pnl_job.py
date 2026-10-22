"""The monitor's nightly joint P&L pass (#397 (b), monitor.joint_pnl): due on trading days after the NAV pass, waits
for the marks, NIFTY's close and the value history until DEADLINE, claims its slot, writes one row, never twice.
Synthetic fixtures only; no network."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time

import pytest
from test_joint_pnl_store import D1, NOT_CONNECTED, _hist, _setup, db  # noqa: F401

from finresearch.fincalc.dates import IST
from finresearch.monitor import joint_pnl as job
from finresearch.monitor.jobs import Deps as _Deps


def Deps(**kw):  # noqa: N802
    return _Deps(ipo_detail=None, quote=None, **kw)


def _at(t: time, d: date = D1) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def test_due_on_trading_days_after_run_after_only():
    assert job.due_slot(_at(time(22, 0)), set()) is None
    assert job.due_slot(_at(job.RUN_AFTER), set()) == f"joint_pnl:{D1.isoformat()}"
    assert job.due_slot(_at(time(23, 30), date(2026, 10, 24)), set()) is None  # a Saturday
    assert job.due_slot(_at(time(23, 30)), {D1}) is None  # a holiday


def test_an_unknown_holiday_list_fails_closed(monkeypatch):
    from finresearch.adapters import nse_holidays
    from finresearch.monitor import market_days

    monkeypatch.setattr(nse_holidays, "known_trading_holidays", lambda day: None)
    monkeypatch.setattr(market_days, "unknown_holidays", lambda day, j: None)
    assert job.due_slot(_at(time(23, 30))) is None


def _deps(hist=None, *, boom: bool = False):
    async def build(holdings, today):
        if boom:
            raise RuntimeError("network down")
        return _hist() if hist is None else hist

    return Deps(joint_pnl=True, joint_history=build)


def _rows():
    from finresearch.db import session_scope
    from finresearch.db.models import JointPnlDay

    with session_scope() as s:
        return {r.day: r.usable for r in s.query(JointPnlDay)}


def _slot():
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot

    with session_scope() as s:
        r = s.get(AlertEvalSlot, f"joint_pnl:{D1.isoformat()}")
        return None if r is None else dict(r.result or {})


def test_a_ready_evening_writes_one_row_and_the_slot_holds_counts_only(db):  # noqa: F811
    _setup()
    res = asyncio.run(job.joint_pnl_step(_deps(), _at(time(23, 20)), set()))
    assert res["joint_pnl"]["written"] is True and _rows()[D1] is True
    got = _slot()
    assert got["status"] == "done" and set(got) <= {"status", "attempts", "written", "usable", "reasons",
                                                    "snapshot_complete"}  # fmt: skip
    assert asyncio.run(job.joint_pnl_step(_deps(), _at(time(23, 25)), set())) == {}  # claimed: never twice


def test_it_waits_for_its_inputs_until_the_deadline_then_writes_the_unknowns(db):  # noqa: F811
    _setup(d1_slot=None)  # tonight's marks are not written
    assert asyncio.run(job.joint_pnl_step(_deps(), _at(time(23, 20)), set())) == {} and _slot() is None
    asyncio.run(job.joint_pnl_step(_deps(), _at(job.DEADLINE), set()))
    assert _rows()[D1] is False  # written, unusable with the reason


def test_a_failed_history_build_fails_the_slot_and_writes_nothing(db):  # noqa: F811
    _setup()
    asyncio.run(job.joint_pnl_step(_deps(boom=True), _at(time(23, 20)), set()))
    assert _slot()["status"] == "failed" and D1 not in _rows()


def test_the_pass_is_off_by_default_and_on_in_the_live_app():
    assert Deps().joint_pnl is False and Deps().joint_history is None
    live = _Deps.live()
    assert live.joint_pnl is True and live.joint_history is not None


@pytest.mark.parametrize("flag", [False, True])
async def test_tick_spawns_the_pass_only_with_its_flag(db, monkeypatch, flag):  # noqa: F811
    from finresearch.monitor import scheduler

    seen = []
    monkeypatch.setattr(scheduler, "_spawn_joint_pnl", lambda deps, now: seen.append(now))
    await scheduler.tick(Deps(joint_pnl=flag), _at(time(23, 20)))
    assert bool(seen) is flag


async def test_the_spawned_pass_runs_the_step(monkeypatch):
    from finresearch.monitor import scheduler

    seen = []

    async def fake(deps, now, holidays=None):
        seen.append(now)
        return {}

    monkeypatch.setattr(job, "joint_pnl_step", fake)
    monkeypatch.setattr(scheduler, "_BACKGROUND", {})
    monkeypatch.setattr(job, "due_slot", lambda now, holidays=None: "joint_pnl:x")
    scheduler._spawn_joint_pnl(Deps(joint_pnl=True), _at(time(23, 20)))
    await scheduler._BACKGROUND["joint_pnl"]
    assert seen == [_at(time(23, 20))]

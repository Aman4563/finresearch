"""finresearch.batchwindow: research batches stay out of 09:00-15:45 IST on trading days, with a CPU/I/O budget
(#375). Offline: fixed clocks, explicit holiday sets, tiny child processes."""

from __future__ import annotations

import sys
from datetime import date, datetime

import pytest

from finresearch import batchwindow as bw
from finresearch.fincalc.dates import IST

WED = date(2026, 10, 21)
HOLIDAY = date(2026, 10, 20)  # a made-up Tuesday holiday for the test


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


@pytest.mark.parametrize(("now", "inside"), [
    (at(WED, 8, 59), False), (at(WED, 9, 0), True), (at(WED, 12, 0), True), (at(WED, 15, 44), True),
    (at(WED, 15, 45), False), (at(WED, 22, 0), False),
    (at(date(2026, 10, 24), 11, 0), False),  # Saturday
    (at(HOLIDAY, 11, 0), False),
])  # fmt: skip
def test_the_quiet_window_is_0900_to_1545_ist_on_trading_days(now, inside):
    assert bw.in_market_hours(now, {HOLIDAY}) is inside


def test_an_unknown_holiday_list_counts_as_a_trading_day():
    """Unknown must protect the live app: a weekday with no cached list is treated as a session."""
    assert bw.in_market_hours(at(WED, 11, 0), bw.UNKNOWN) is True


def test_a_utc_clock_is_converted_to_ist():
    from datetime import UTC

    assert bw.in_market_hours(datetime(2026, 10, 21, 4, 0, tzinfo=UTC), set()) is True  # 09:30 IST
    assert bw.in_market_hours(datetime(2026, 10, 21, 10, 30, tzinfo=UTC), set()) is False  # 16:00 IST


def test_resume_at_is_the_end_of_the_window():
    assert bw.resume_at(at(WED, 10, 0), set()) == at(WED, 15, 45)
    assert bw.resume_at(at(WED, 16, 0), set()) is None


def test_a_job_started_during_market_hours_is_deferred_and_never_runs(tmp_path):
    marker = tmp_path / "ran"
    res = bw.run_batch([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], clock=lambda: at(WED, 10, 0),
                       holidays=set())  # fmt: skip
    assert res.deferred is True and res.returncode is None and res.resume_at == at(WED, 15, 45)
    assert not marker.exists()


def test_a_job_outside_market_hours_runs_with_the_budget(tmp_path):
    marker = tmp_path / "ran"
    res = bw.run_batch([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], clock=lambda: at(WED, 20, 0),
                       holidays=set(), poll=0.05)  # fmt: skip
    assert res.deferred is False and res.returncode == 0 and marker.exists()


def test_wait_sleeps_until_the_window_ends_then_runs(tmp_path):
    marker = tmp_path / "ran"
    t = {"now": at(WED, 15, 0)}
    slept = []

    def sleep(s):
        slept.append(s)
        t["now"] = at(WED, 15, 45)

    res = bw.run_batch([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], clock=lambda: t["now"],
                       holidays=set(), wait=True, sleep=sleep, poll=0.05)  # fmt: skip
    assert slept and slept[0] == 45 * 60 and res.returncode == 0 and marker.exists()


def test_budget_argv_lowers_cpu_and_io_priority():
    assert bw.budget_argv(["x"], platform="darwin", have_taskpolicy=True) == [
        "/usr/sbin/taskpolicy",
        "-b",
        "x",
    ]
    assert bw.budget_argv(["x"], platform="linux", have_taskpolicy=False) == ["nice", "-n", "15", "x"]


def test_a_running_job_is_paused_at_the_open_and_resumed_after_the_window(tmp_path):
    """A batch that runs into 09:00 is stopped (SIGSTOP), not left competing with the live app, and continued at
    15:45."""
    clocks = iter([at(WED, 8, 59), at(WED, 9, 1), at(WED, 9, 2), at(WED, 15, 46)])
    last = {"t": at(WED, 15, 46)}

    def clock():
        last["t"] = next(clocks, last["t"])
        return last["t"]

    events = []
    res = bw.run_batch([sys.executable, "-c", "import time; time.sleep(0.6)"], clock=clock, holidays=set(),
                       poll=0.1, on_signal=events.append)  # fmt: skip
    assert res.returncode == 0 and events == ["paused", "resumed"]


@pytest.mark.parametrize(("hh", "code", "ran"), [(10, 75, False), (20, 3, True)])
def test_cli_batch_run_defers_with_exit_75_or_passes_the_exit_code(env, tmp_path, monkeypatch, hh, code, ran):
    from typer.testing import CliRunner

    from finresearch.cli import app

    monkeypatch.setattr(
        bw, "now_ist", lambda: at(WED, hh, 0)
    )  # no cached holiday list: a weekday is a session
    marker = tmp_path / "ran"
    r = CliRunner().invoke(app, ["batch", "run", "--", sys.executable, "-c",
                                 f"open({str(marker)!r}, 'w'); raise SystemExit(3)"])  # fmt: skip
    assert r.exit_code == code, r.output
    assert marker.exists() is ran
    if not ran:
        assert "deferred" in r.output and "15:45" in r.output

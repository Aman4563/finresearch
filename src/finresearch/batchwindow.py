"""Research batches run outside market hours with a CPU and I/O budget (#375, roadmap P1.13).

Backfills, SVI fits, CSCV/SPA bootstraps and simulations share the 16 GB Mac with the live app, which is busiest
in the session. `run_batch` (CLI: `finresearch batch run -- <command>`) therefore:

- defers a job started between 09:00 and 15:45 IST on a trading day (or, with `wait`, sleeps until 15:45);
- runs it under macOS background QoS (`taskpolicy -b`: throttled CPU, disk and network I/O) or `nice` elsewhere;
- stops it (SIGSTOP) if it is still running when the window opens and continues it (SIGCONT) when it closes.

A weekday whose NSE holiday list is not cached counts as a trading day: unknown protects the live app (the opposite
of the after-close data jobs, which fail closed because running on a holiday stores wrong data, #247).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime

from finresearch.fincalc.dates import IST, is_business_day, now_ist, to_ist

OPEN, CLOSE = dtime(9, 0), dtime(15, 45)
UNKNOWN = None  # the holiday list is not cached for the year
_LOOKUP = object()
TASKPOLICY = "/usr/sbin/taskpolicy"


def _holidays(day: date, holidays):
    if holidays is not _LOOKUP:
        return holidays
    from finresearch.adapters.nse_holidays import known_trading_holidays

    return known_trading_holidays(day)


def in_market_hours(now: datetime, holidays: Collection[date] | object | None = _LOOKUP) -> bool:
    """True from 09:00 to before 15:45 IST on a trading day (an unknown holiday list counts as trading)."""
    t = to_ist(now)
    if not OPEN <= t.time() < CLOSE:
        return False
    hol = _holidays(t.date(), holidays)
    return t.weekday() < 5 if hol is UNKNOWN else is_business_day(t.date(), hol)


def resume_at(now: datetime, holidays: Collection[date] | object | None = _LOOKUP) -> datetime | None:
    """When a deferred job may start: the end of today's window, or None when `now` is outside it."""
    if not in_market_hours(now, holidays):
        return None
    d = to_ist(now).date()
    return datetime(d.year, d.month, d.day, CLOSE.hour, CLOSE.minute, tzinfo=IST)


def budget_argv(
    cmd: Sequence[str], *, platform: str = sys.platform, have_taskpolicy: bool | None = None
) -> list[str]:
    """The command under a lowered CPU and I/O priority."""
    if have_taskpolicy is None:
        have_taskpolicy = os.access(TASKPOLICY, os.X_OK)
    if platform == "darwin" and have_taskpolicy:
        return [TASKPOLICY, "-b", *cmd]  # background QoS: CPU and I/O throttled behind interactive work
    return ["nice", "-n", "15", *cmd]


@dataclass
class BatchResult:
    deferred: bool
    returncode: int | None = None
    resume_at: datetime | None = None
    paused_s: float = 0.0


def run_batch(cmd: Sequence[str], *, wait: bool = False, cpu_seconds: int | None = None,
              clock: Callable[[], datetime] | None = None, holidays: Collection[date] | object | None = _LOOKUP,
              sleep: Callable[[float], None] = time.sleep, poll: float = 30.0,
              on_signal: Callable[[str], None] | None = None) -> BatchResult:  # fmt: skip
    """Run `cmd` outside market hours with the budget; see the module docstring."""
    clock = clock or (lambda: now_ist())  # looked up per call (tests replace the module's clock)
    until = resume_at(clock(), holidays)
    if until is not None:
        if not wait:
            return BatchResult(deferred=True, resume_at=until)
        while (until := resume_at(clock(), holidays)) is not None:
            sleep(max(1.0, (until - clock()).total_seconds()))

    def limits() -> None:  # in the child: a hard CPU-time cap when asked
        if cpu_seconds:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))

    # its own process group, so a pause stops the job's children too (e.g. `uv run` -> python)
    proc = subprocess.Popen(budget_argv(cmd), preexec_fn=limits if cpu_seconds else None,
                            start_new_session=True)  # fmt: skip

    def group(sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)

    paused, paused_s, since = False, 0.0, 0.0
    try:
        while proc.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=poll)  # returns at once when the job ends
                break
            inside = in_market_hours(clock(), holidays)
            if inside and not paused:
                group(signal.SIGSTOP)
                paused, since = True, time.monotonic()
                on_signal and on_signal("paused")
            elif paused and not inside:
                group(signal.SIGCONT)
                paused, paused_s = False, paused_s + time.monotonic() - since
                on_signal and on_signal("resumed")
        return BatchResult(deferred=False, returncode=proc.returncode, paused_s=paused_s)
    finally:
        if proc.poll() is None:  # interrupted: never leave a stopped child behind
            group(signal.SIGCONT)
            group(signal.SIGTERM)

"""#367: the nightly event-calendar refresh job (monitor.eventcal): its evening window outside market hours, one slot
a night, undecided sources recorded as skipped with no request and no retry, a failed source retried, and the
scheduler wiring (Deps flag, spawn hook, schedule entry)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from finresearch.eventcal import calendar as cal
from finresearch.eventcal import refresh
from finresearch.monitor import eventcal as job

AFTER = datetime(2026, 10, 23, 15, 45, tzinfo=UTC)  # Fri 21:15 IST
BEFORE = datetime(2026, 10, 23, 15, 15, tzinfo=UTC)  # 20:45 IST
SUNDAY = datetime(2026, 10, 25, 15, 45, tzinfo=UTC)


def test_due_slot_every_evening_after_the_market():
    # 20:45 IST: before the full run, inside the 16:30 daytime window of RBI's feed (owner, 26-Oct-2026)
    assert job.due_slot(BEFORE) == "eventcal-rss:2026-10-23T1630"
    assert job.due_slot(AFTER) == "eventcal:2026-10-23"
    assert job.due_slot(SUNDAY) == "eventcal:2026-10-25"  # RBI and MoSPI do not follow NSE's calendar
    # 10:30 IST: only the 06:00 daytime window of RBI's RSS feed is open (owner, 26-Oct-2026: four reads a day)
    assert job.due_slot(datetime(2026, 10, 23, 5, 0, tzinfo=UTC)) == "eventcal-rss:2026-10-23T0600"
    assert (
        job.due_slot(datetime(2026, 10, 23, 0, 0, tzinfo=UTC)) is None
    )  # 05:30 IST, before the first window
    assert (
        job.due_slot(datetime(2026, 10, 23, 11, 15, tzinfo=UTC)) == "eventcal-rss:2026-10-23T1630"
    )  # 16:45 IST
    assert job.RUN_AFTER.hour >= 16  # never inside 09:00-16:00 IST


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE calendar_event, calendar_coverage"))
        s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'eventcal%'"))
    yield env


SOURCES = ["rbi_rss", "india_cpi_rule", "mospi_cpi", "nifty_review_rule", "nse_circulars"]  # #481
GATED = ["rbi_rss", "mospi_cpi", "nse_circulars"]  # read only with a data-rights decision
RULES = ["india_cpi_rule", "nifty_review_rule"]  # published rules: no request, no decision needed


@pytest.fixture
def undecided(monkeypatch):
    """The gated sources without a decision, whatever the owner has recorded since."""
    from finresearch import data_rights

    for name in GATED:
        monkeypatch.delitem(data_rights.DECISIONS, name, raising=False)


async def test_undecided_sources_are_skipped_without_requests_and_not_retried(clean, undecided):
    import respx

    with respx.mock(assert_all_called=False) as mock:
        res = await job.eventcal_step(AFTER)
        assert not mock.calls  # respx clears its calls on exit
    assert sorted(res["eventcal"]["skipped"]) == sorted(GATED) and sorted(
        res["eventcal"]["stored"]
    ) == sorted(RULES)
    assert await job.eventcal_step(AFTER) == {}  # the night's slot is taken


async def test_a_failed_source_marks_the_night_failed_and_stores_nothing(clean, monkeypatch):
    from finresearch import data_rights
    from finresearch.monitor import disclosures

    monkeypatch.setitem(data_rights.DECISIONS, "rbi_rss", data_rights.Decision(
        "rbi_rss", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip

    async def broken(known, now):
        raise RuntimeError("HTTP 503")

    finished: list[bool] = []
    real = disclosures.finish
    monkeypatch.setattr(disclosures, "finish", lambda slot, r, now, failed: (finished.append(failed),
                                                                            real(slot, r, now, failed)))  # fmt: skip
    res = await job.eventcal_step(AFTER, sources=[refresh.Source("rbi_rss", broken)])
    assert res["eventcal"]["failed"] == {"rbi_rss": "RuntimeError: HTTP 503"} and finished == [True]
    from finresearch.db import session_scope
    from finresearch.eventcal import store

    with session_scope() as s:
        assert store.load(s) == ([], [])


async def test_a_decided_source_is_stored_known_from_the_run(clean, monkeypatch):
    from finresearch import data_rights

    monkeypatch.setitem(data_rights.DECISIONS, "rbi_rss", data_rights.Decision(
        "rbi_rss", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip
    url = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid=90002"

    async def reader(known, now):
        return refresh.Found("rbi_rss", events=[cal.Event(
            type="rbi_mpc", tier=1, scope="IN", day=date(2027, 4, 7), at=None, title="t", source_url=url,
            known_since=now)])  # fmt: skip

    res = await job.eventcal_step(AFTER, sources=[refresh.Source("rbi_rss", reader)])
    assert res["eventcal"]["stored"]["rbi_rss"]["events"] == 1
    from finresearch.db import session_scope
    from finresearch.eventcal import store

    with session_scope() as s:
        assert [e.known_since for e in store.load(s)[0]] == [AFTER]


def test_the_live_monitor_runs_it_and_the_schedule_names_it():
    from finresearch.monitor import scheduler
    from finresearch.monitor.jobs import Deps
    from finresearch.suggest.profile import WatchWindows

    assert Deps(None, None).eventcal is False  # tests opt in; Deps.live() turns it on
    assert Deps.live().eventcal is True
    sj = scheduler.schedule_json(WatchWindows(), running=True)
    assert sj["eventcal"] == {"after": "21:00", "trading_days": False,
                              "sources": SOURCES}  # fmt: skip


async def test_tick_spawns_the_job_only_when_due(monkeypatch):
    import asyncio

    from finresearch.monitor import scheduler

    calls: list[datetime] = []

    async def fake_step(now, **kw):
        calls.append(now)
        return {}

    monkeypatch.setattr(job, "eventcal_step", fake_step)
    monkeypatch.setattr(scheduler, "_BACKGROUND", {})
    scheduler._spawn_eventcal(datetime(2026, 10, 23, 0, 0, tzinfo=UTC))  # 05:30 IST: nothing due before 06:00
    scheduler._spawn_eventcal(AFTER)
    await asyncio.gather(*scheduler._BACKGROUND.values())
    assert calls == [AFTER]


def test_cli_refresh_says_which_sources_wait_for_a_decision(clean, undecided):
    import respx
    from typer.testing import CliRunner

    from finresearch import cli

    with respx.mock(assert_all_called=False) as mock:
        r = CliRunner().invoke(cli.app, ["events", "refresh"])
        assert not mock.calls  # respx clears its calls on exit
    assert r.exit_code == 0, r.output
    assert all(f"{name}: skipped" in r.output for name in GATED), r.output
    assert all(f"{name}: " in r.output and "new events" in r.output for name in RULES), r.output


def test_cli_refresh_exits_non_zero_when_a_decided_source_fails(clean, monkeypatch):
    from typer.testing import CliRunner

    from finresearch import cli

    async def broken(known, now):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(refresh, "sources", lambda: [refresh.Source("rbi_rss", broken)])
    from finresearch import data_rights

    monkeypatch.setitem(data_rights.DECISIONS, "rbi_rss", data_rights.Decision(
        "rbi_rss", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip
    r = CliRunner().invoke(cli.app, ["events", "refresh"])
    assert r.exit_code == 1 and "rbi_rss: failed" in r.output


def test_cli_refresh_prints_the_notices_to_review(clean, monkeypatch):
    from typer.testing import CliRunner

    from finresearch import cli, data_rights

    async def reader(known, now):
        return refresh.Found(
            "nse_circulars", notices=["NSE/FAOP/99003 of 2026-10-22: Revision in Expiry Day (link)"]
        )

    monkeypatch.setattr(refresh, "sources", lambda: [refresh.Source("nse_circulars", reader)])
    monkeypatch.setitem(data_rights.DECISIONS, "nse_circulars", data_rights.Decision(
        "nse_circulars", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip
    r = CliRunner().invoke(cli.app, ["events", "refresh"])
    assert r.exit_code == 0, r.output
    assert "nse_circulars: to review: NSE/FAOP/99003 of 2026-10-22: Revision in Expiry Day (link)" in r.output


async def test_a_daytime_window_reads_rbis_feed_alone(clean, monkeypatch):
    """Owner, 26-Oct-2026: RBI's feed keeps only its 10 newest items, so it is read at 06:00, 12:00 and 16:30 IST too.
    Those windows read the feed alone, never NSE, and each is claimed once."""
    from finresearch import data_rights

    monkeypatch.setitem(data_rights.DECISIONS, "rbi_rss", data_rights.Decision(
        "rbi_rss", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip
    called: list[str] = []

    def reader(name):
        async def read(known, now):
            called.append(name)
            return refresh.Found(name)

        return read

    srcs = [refresh.Source(n, reader(n)) for n in ("rbi_rss", "nse_circulars")]
    monkeypatch.setitem(data_rights.DECISIONS, "nse_circulars", data_rights.Decision(
        "nse_circulars", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip
    noon = datetime(2026, 10, 23, 7, 0, tzinfo=UTC)  # 12:30 IST
    res = await job.eventcal_step(noon, sources=srcs)
    assert called == ["rbi_rss"] and not res["eventcal"]["failed"]
    assert await job.eventcal_step(noon, sources=srcs) == {}  # the window is claimed once
    called.clear()
    await job.eventcal_step(AFTER, sources=srcs)  # 21:15 IST: the full nightly run reads every source
    assert called == ["rbi_rss", "nse_circulars"]

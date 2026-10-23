"""#367 refresh: what a nightly read stores. New rows are known from the fetch time (never back-dated, whatever the
document's date), a moved meeting keeps its old date until the notice is seen, a newer release calendar withdraws
dates it no longer lists, a failed or refused source stores nothing, and coverage never grows from a failure."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import text

from finresearch import data_rights
from finresearch.eventcal import calendar as cal
from finresearch.eventcal import refresh, store
from finresearch.fincalc.dates import IST

NOW = datetime(2026, 10, 23, 21, 0, tzinfo=IST)
MARCH = datetime(2026, 3, 24, 23, 59, tzinfo=IST)  # the document's own date: never used as known_since
URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid=90002"


def _ev(day: date, known: datetime = MARCH, kind: str = "rbi_mpc") -> cal.Event:
    return cal.Event(type=kind, tier=1 if kind == "rbi_mpc" else 2, scope="IN", day=day, at=None, title=kind,
                     source_url=URL, known_since=known)  # fmt: skip


def _cov(through: date, known: datetime = MARCH) -> cal.Coverage:
    return cal.Coverage(type="rbi_mpc", scope="IN", through=through, source_url=URL, known_since=known)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE calendar_event, calendar_coverage"))
        s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'eventcal:%'"))
    yield env


def _load():
    from finresearch.db import session_scope

    with session_scope() as s:
        return store.load(s)


def _apply(found: refresh.Found, now: datetime = NOW) -> None:
    from finresearch.db import session_scope

    with session_scope() as s:
        refresh.apply(s, found, now)


def test_new_rows_are_known_from_the_fetch_never_from_the_document_date(clean):
    _apply(
        refresh.Found("rbi_mpc_schedule", events=[_ev(date(2027, 6, 4))], coverage=[_cov(date(2028, 3, 31))])
    )
    ev, cov = _load()
    assert [(e.day, e.known_since) for e in ev] == [(date(2027, 6, 4), NOW)]
    assert ev[0].known_since_url == URL
    assert [(c.through, c.known_since) for c in cov] == [(date(2028, 3, 31), NOW)]


def test_a_row_already_known_earlier_keeps_its_earlier_known_since(clean):
    from finresearch.db import session_scope

    with session_scope() as s:
        store.upsert(s, [_ev(date(2026, 12, 4))], [])  # the seed's row, known since March
    _apply(refresh.Found("rbi_mpc_schedule", events=[_ev(date(2026, 12, 4))], coverage=[]))
    assert [e.known_since for e in _load()[0]] == [MARCH]


def test_a_moved_meeting_keeps_the_old_date_until_the_notice_is_seen(clean):
    from finresearch.db import session_scope

    with session_scope() as s:
        store.upsert(s, [_ev(date(2027, 6, 4))], [])
    _apply(refresh.Found("rbi_mpc_schedule", events=[_ev(date(2027, 6, 5))],
                         moves=[refresh.Move("rbi_mpc", "IN", date(2027, 6, 4), date(2027, 6, 5))]))  # fmt: skip
    ev, _ = _load()
    full = [cal.Coverage(type=k, scope="IN", through=date(2028, 3, 31), source_url=URL, known_since=MARCH)
            for k in cal.TIER1_TYPES]  # fmt: skip
    before, after = datetime(2026, 10, 23, 20, 59, tzinfo=IST), NOW
    win = {"start": date(2027, 6, 1), "end": date(2027, 6, 10)}
    assert [r["day"] for r in cal.window_check(ev, full, as_of=before, **win)["tier1"]] == ["2027-06-04"]
    assert [r["day"] for r in cal.window_check(ev, full, as_of=after, **win)["tier1"]] == ["2027-06-05"]
    old = next(e for e in ev if e.day == date(2027, 6, 4))
    assert old.withdrawn_since == NOW  # the time the notice was seen, never earlier


def test_a_newer_release_calendar_withdraws_future_dates_it_no_longer_lists(clean):
    cpi = lambda d: _ev(d, kind="india_cpi")  # noqa: E731
    _apply(refresh.Found("mospi_release_calendar", events=[cpi(date(2026, 11, 12)), cpi(date(2026, 12, 12))]))
    later = datetime(2026, 11, 2, 21, 0, tzinfo=IST)
    _apply(refresh.Found("mospi_release_calendar", events=[cpi(date(2026, 11, 13)), cpi(date(2026, 12, 12))],
                         replaces=[refresh.Replace("india_cpi", "IN", date(2026, 11, 2), date(2027, 3, 31))]),
           later)  # fmt: skip
    got = {e.day: e.withdrawn_since for e in _load()[0]}
    assert got == {date(2026, 11, 12): later, date(2026, 11, 13): None, date(2026, 12, 12): None}


async def test_a_failed_source_stores_nothing_and_coverage_stays_where_it_was(clean, monkeypatch):
    monkeypatch.setitem(data_rights.DECISIONS, "rbi_mpc_schedule", data_rights.Decision(
        "rbi_mpc_schedule", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip
    from finresearch.db import session_scope

    with session_scope() as s:
        store.upsert(s, [], [_cov(date(2027, 3, 31))])

    async def broken(known: refresh.Known, now: datetime) -> refresh.Found:
        raise RuntimeError("HTTP 503")

    res = await refresh.run([refresh.Source("rbi_mpc_schedule", broken)], NOW)
    assert res["failed"] == {"rbi_mpc_schedule": "RuntimeError: HTTP 503"} and res["stored"] == {}
    ev, cov = _load()
    assert ev == [] and [c.through for c in cov] == [date(2027, 3, 31)]
    # past the last coverage the check reads unknown, never "no events"
    chk = cal.window_check(ev, cov, start=date(2027, 3, 25), end=date(2027, 4, 15), as_of=NOW)
    assert chk["status"] == "unknown" and "rbi_mpc" in chk["uncovered"]


async def test_a_source_without_a_data_rights_decision_is_skipped_unread(clean):
    called = []

    async def reader(known: refresh.Known, now: datetime) -> refresh.Found:
        called.append(now)
        return refresh.Found("nope")

    res = await refresh.run([refresh.Source("no_such_dataset_367", reader)], NOW)
    assert called == [] and "no_such_dataset_367" in res["skipped"] and res["failed"] == {}


async def test_a_decided_source_is_stored_and_sees_what_is_already_known(clean, monkeypatch):
    monkeypatch.setitem(data_rights.DECISIONS, "rbi_mpc_schedule", data_rights.Decision(
        "rbi_mpc_schedule", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip
    from finresearch.db import session_scope

    with session_scope() as s:
        store.upsert(s, [], [_cov(date(2027, 3, 31))])
    seen: list[refresh.Known] = []

    async def reader(known: refresh.Known, now: datetime) -> refresh.Found:
        seen.append(known)
        return refresh.Found(
            "rbi_mpc_schedule", events=[_ev(date(2027, 6, 4))], coverage=[_cov(date(2028, 3, 31))]
        )

    res = await refresh.run([refresh.Source("rbi_mpc_schedule", reader)], NOW)
    assert res["stored"] == {
        "rbi_mpc_schedule": {"events": 1, "coverage": 1, "withdrawn": 0, "reinstated": 0}
    }
    assert seen[0].covered["rbi_mpc"] == date(2027, 3, 31) and URL in seen[0].urls


SOURCES = ["rbi_mpc_schedule", "mospi_release_calendar", "nifty_indices_press", "nse_circulars"]


@pytest.fixture
def undecided(monkeypatch):
    """The refresh sources without a decision, whatever the owner has recorded since (so a decision never turns
    these tests red)."""
    for name in SOURCES:
        monkeypatch.delitem(data_rights.DECISIONS, name, raising=False)


def test_every_refresh_source_has_a_recorded_data_rights_state():
    # the owner records each decision (data_rights.DECISIONS); until then it is listed as PENDING with what was read
    assert [s.dataset for s in refresh.sources()] == SOURCES
    for name in SOURCES:
        assert name in data_rights.DECISIONS or name in data_rights.PENDING, name


def test_an_undecided_source_is_refused_with_what_was_read(undecided):
    for name in SOURCES:
        if name in data_rights.PENDING:
            with pytest.raises(data_rights.NoDecision, match="Owner decision needed"):
                data_rights.require(name)


async def test_the_live_sources_make_no_request_while_undecided(clean, undecided):
    import respx

    with respx.mock(assert_all_called=False) as mock:
        res = await refresh.run(refresh.sources(), NOW)
    assert not mock.calls and sorted(res["skipped"]) == sorted(SOURCES)
    assert res["stored"] == {} and res["failed"] == {}


def test_cpi_and_gdp_are_context_events_with_titles():
    assert {"india_cpi", "india_gdp"} <= set(cal.TITLES) and not {"india_cpi", "india_gdp"} & set(
        cal.TIER1_TYPES
    )


def test_a_date_listed_again_after_a_move_is_reinstated_never_left_out(clean):
    # moved away (4 -> 5 June), then a later notice moves it back: the 4th must be usable again, or a complete
    # schedule would read "no tier-1 event" on a real meeting day
    from finresearch.db import session_scope

    with session_scope() as s:
        store.upsert(s, [_ev(date(2027, 6, 4))], [])
    _apply(refresh.Found("rbi_mpc_schedule", events=[_ev(date(2027, 6, 5))],
                         moves=[refresh.Move("rbi_mpc", "IN", date(2027, 6, 4), date(2027, 6, 5))]))  # fmt: skip
    back = datetime(2026, 11, 2, 21, 0, tzinfo=IST)
    _apply(refresh.Found("rbi_mpc_schedule", events=[_ev(date(2027, 6, 4))],
                         moves=[refresh.Move("rbi_mpc", "IN", date(2027, 6, 5), date(2027, 6, 4))]), back)  # fmt: skip
    got = {e.day: (e.known_since, e.withdrawn_since) for e in _load()[0]}
    assert got[date(2027, 6, 5)] == (NOW, back)
    assert got[date(2027, 6, 4)][1] is None  # reinstated (known since March, as first seen)


async def test_notices_are_reported_per_source_and_store_nothing(clean, monkeypatch):
    monkeypatch.setitem(data_rights.DECISIONS, "nse_circulars", data_rights.Decision(
        "nse_circulars", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip

    async def reader(known: refresh.Known, now: datetime) -> refresh.Found:
        return refresh.Found(
            "nse_circulars", notices=["NSE/FAOP/99001 of 2026-10-22: Revision in expiry day (url)"]
        )

    res = await refresh.run([refresh.Source("nse_circulars", reader)], NOW)
    assert res["notices"] == {"nse_circulars": ["NSE/FAOP/99001 of 2026-10-22: Revision in expiry day (url)"]}
    assert res["stored"]["nse_circulars"] == {"events": 0, "coverage": 0, "withdrawn": 0, "reinstated": 0}
    assert _load() == ([], [])

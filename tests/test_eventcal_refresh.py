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
    _apply(refresh.Found("rbi_rss", events=[_ev(date(2027, 6, 4))], coverage=[_cov(date(2028, 3, 31))]))
    ev, cov = _load()
    assert [(e.day, e.known_since) for e in ev] == [(date(2027, 6, 4), NOW)]
    assert ev[0].known_since_url == URL
    assert [(c.through, c.known_since) for c in cov] == [(date(2028, 3, 31), NOW)]


def test_a_row_already_known_earlier_keeps_its_earlier_known_since(clean):
    from finresearch.db import session_scope

    with session_scope() as s:
        store.upsert(s, [_ev(date(2026, 12, 4))], [])  # the seed's row, known since March
    _apply(refresh.Found("rbi_rss", events=[_ev(date(2026, 12, 4))], coverage=[]))
    assert [e.known_since for e in _load()[0]] == [MARCH]


def test_a_moved_meeting_keeps_the_old_date_until_the_notice_is_seen(clean):
    from finresearch.db import session_scope

    with session_scope() as s:
        store.upsert(s, [_ev(date(2027, 6, 4))], [])
    _apply(refresh.Found("rbi_rss", events=[_ev(date(2027, 6, 5))],
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
    monkeypatch.setitem(data_rights.DECISIONS, "rbi_rss", data_rights.Decision(
        "rbi_rss", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip
    from finresearch.db import session_scope

    with session_scope() as s:
        store.upsert(s, [], [_cov(date(2027, 3, 31))])

    async def broken(known: refresh.Known, now: datetime) -> refresh.Found:
        raise RuntimeError("HTTP 503")

    res = await refresh.run([refresh.Source("rbi_rss", broken)], NOW)
    assert res["failed"] == {"rbi_rss": "RuntimeError: HTTP 503"} and res["stored"] == {}
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
    monkeypatch.setitem(data_rights.DECISIONS, "rbi_rss", data_rights.Decision(
        "rbi_rss", "accept-personal", "2026-10-23", "test", "test"))  # fmt: skip
    from finresearch.db import session_scope

    with session_scope() as s:
        store.upsert(s, [], [_cov(date(2027, 3, 31))])
    seen: list[refresh.Known] = []

    async def reader(known: refresh.Known, now: datetime) -> refresh.Found:
        seen.append(known)
        return refresh.Found("rbi_rss", events=[_ev(date(2027, 6, 4))], coverage=[_cov(date(2028, 3, 31))])

    res = await refresh.run([refresh.Source("rbi_rss", reader)], NOW)
    assert res["stored"] == {"rbi_rss": {"events": 1, "coverage": 1, "withdrawn": 0, "reinstated": 0}}
    assert seen[0].covered["rbi_mpc"] == date(2027, 3, 31) and URL in seen[0].urls


# the owner's decisions of 23-Oct-2026 (#481): RBI's RSS feed, two published rules, the CPI API and NSE's circulars
SOURCES = ["rbi_rss", "india_cpi_rule", "mospi_cpi", "nifty_review_rule", "nse_circulars"]
GATED = ["rbi_rss", "mospi_cpi", "nse_circulars"]  # each read needs its data-rights decision
RULES = ["india_cpi_rule", "nifty_review_rule"]  # computed from published rules: nothing is requested
DROPPED = [
    "rbi_mpc_schedule",
    "mospi_release_calendar",
    "nifty_indices_press",
]  # #478's sources, dropped (#481)


@pytest.fixture
def undecided(monkeypatch):
    """The gated sources without a decision, whatever the owner has recorded since."""
    for name in GATED:
        monkeypatch.delitem(data_rights.DECISIONS, name, raising=False)


def test_the_refresh_reads_only_the_sources_the_owner_accepted():
    srcs = refresh.sources()
    assert [s.dataset for s in srcs] == SOURCES
    assert [s.dataset for s in srcs if s.rule] == RULES
    for name in GATED:
        assert data_rights.require(name).choice == "accept-personal"
    for name in DROPPED:
        assert data_rights.DECISIONS[name].choice == "drop"


def test_the_dropped_sources_readers_are_gone():
    import importlib.util

    for module in ("finresearch.eventcal.mospi", "finresearch.eventcal.nifty_indices"):
        assert importlib.util.find_spec(module) is None, module


async def test_undecided_sources_make_no_request_and_rules_still_run(clean, undecided):
    import respx

    with respx.mock(assert_all_called=False) as mock:
        res = await refresh.run(refresh.sources(), NOW)
    assert not mock.calls and sorted(res["skipped"]) == sorted(GATED) and res["failed"] == {}
    assert sorted(res["stored"]) == sorted(RULES) and all(res["stored"][r]["events"] for r in RULES)
    assert {e.type for e in _load()[0]} == {"india_cpi", "index_rebalance"}


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
    _apply(refresh.Found("rbi_rss", events=[_ev(date(2027, 6, 5))],
                         moves=[refresh.Move("rbi_mpc", "IN", date(2027, 6, 4), date(2027, 6, 5))]))  # fmt: skip
    back = datetime(2026, 11, 2, 21, 0, tzinfo=IST)
    _apply(refresh.Found("rbi_rss", events=[_ev(date(2027, 6, 4))],
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


# ---- every live source in one run: what is requested (respx records it all; a catch-all answers, nothing is sent)

ALLOWED = {("rbi.org.in", "/pressreleases_rss.xml"), ("www.nseindia.com", "/resources/exchange-communication-circulars"),
           ("www.nseindia.com", "/api/circulars"), ("api.mospi.gov.in", "/api/cpi/getCPIIndex"),
           ("api.mospi.gov.in", "/api/cpi/getCPIData")}  # fmt: skip


async def test_no_code_path_requests_rbi_pages_mospi_calendars_or_niftyindices(clean, monkeypatch, tmp_path):
    import json
    from pathlib import Path

    import httpx
    import respx

    from finresearch.adapters import mospi_cpi as mc
    from finresearch.adapters.http import PoliteClient
    from finresearch.db import session_scope
    from finresearch.eventcal import nse, rbi, rules

    async def no_sleep(_s: float) -> None:
        return None

    async def fetch(
        today: date,
    ):  # the real client, cached under tmp_path (not data/), without rate-limit sleeps
        http = PoliteClient(cache_dir=tmp_path, sleep=no_sleep, clock=lambda: 0.0, max_retries=0,
                            verify=mc.tls_context())  # fmt: skip
        try:
            return await mc.MospiCpiClient(http).fetch(today=today)
        finally:
            await http.aclose()

    monkeypatch.setattr(rules, "_fetch_cpi", fetch)
    fix = Path(__file__).parent / "fixtures" / "eventcal"
    feed = (fix / "rbi_rss.xml").read_bytes()
    feed = feed.replace(
        rbi.parse_feed(feed)[-1].body.encode(), b""
    )  # the schedule's text not in the feed yet
    listing = json.loads((fix / "nse_circulars.json").read_text())
    with (
        session_scope() as s
    ):  # an expected CPI release that has come: its confirmation reads MoSPI's CPI API
        store.upsert(s, [cal.Event(type="india_cpi", tier=2, scope="IN", day=date(2026, 11, 12), at=None,
                                   title=f"India CPI release for Oct 2026 ({rules.EXPECTED})",
                                   source_url=rules.CPI_RULE_URL, known_since=MARCH)], [])  # fmt: skip
    old = [{"year": 2025, "month": "December", "subgroup": sg, "index": "190.0", "status": "F"}
           for sg in ("General-Overall", "Health", "Education")]  # fmt: skip
    new = [
        {"year": "2026", "month": "October", "division": "CPI (General)", "group": None, "inflation": "3.10"}
    ]

    def page(rows: list[dict]) -> httpx.Response:
        return httpx.Response(200, json={"statusCode": True, "data": rows, "meta_data": {"totalPages": 1}})

    with respx.mock(assert_all_called=False) as mock:
        mock.get(rbi.FEED).mock(return_value=httpx.Response(200, content=feed))
        mock.get(nse.PAGE).mock(return_value=httpx.Response(200, text="<html/>"))
        mock.get("https://www.nseindia.com/api/circulars").mock(
            return_value=httpx.Response(200, json=listing)
        )
        mock.get(url__startswith=mc.BASE + "getCPIIndex").mock(return_value=page(old))
        mock.get(url__startswith=mc.BASE + "getCPIData").mock(return_value=page(new))
        mock.route().mock(return_value=httpx.Response(404))  # anything else is recorded here, never sent
        res = await refresh.run(refresh.sources(), datetime(2026, 11, 13, 21, 0, tzinfo=IST))
        asked = {(c.request.url.host, c.request.url.path) for c in mock.calls}  # respx clears them on exit
    assert res["failed"] == {} and res["skipped"] == {} and sorted(res["stored"]) == sorted(SOURCES), res
    assert asked <= ALLOWED, asked - ALLOWED
    assert asked >= {("rbi.org.in", "/pressreleases_rss.xml"), ("api.mospi.gov.in", "/api/cpi/getCPIData")}
    assert any("90100" in n for n in res["notices"]["rbi_rss"])  # a notice, never a fetch of the release page
    rows = {(e.type, e.day): e.title for e in _load()[0]}
    assert "published" in rows[("india_cpi", date(2026, 11, 12))]

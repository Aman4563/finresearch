"""#481: RBI's MPC schedule, rescheduling notices and resolutions, read from RBI's press-release RSS feed only
(https://rbi.org.in/pressreleases_rss.xml). The feed is a hand-made copy in tests/fixtures/eventcal/rbi_rss.xml:
synthetic dates and prids, with the real feed's shape (a BOM, CDATA titles, HTML descriptions, zone-less IST pubDates,
lowercase "scripts" links). Only facts and links are kept, the feed is the only request, and gaps in RBI's sequential
release numbers are reported, because the feed lists only its newest ten items."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.http import PoliteClient
from finresearch.eventcal import rbi, refresh
from finresearch.eventcal.calendar import TITLES
from finresearch.fincalc.dates import IST

FEED = (Path(__file__).parent / "fixtures" / "eventcal" / "rbi_rss.xml").read_bytes()
URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid={}"  # the seed's citation form
NOW = datetime(2027, 5, 21, 21, 0, tzinfo=IST)  # the fixture's newest item is of 21-May-2027 17:05


def _item(prid: int) -> rbi.Item:
    return next(i for i in rbi.parse_feed(FEED) if i.prid == prid)


def test_items_take_their_prid_from_the_link_and_their_time_as_ist():
    items = rbi.parse_feed(FEED)
    # feed order (newest first); 88888 is a release cited inside a description, never an item
    assert [i.prid for i in items] == [90109, 90108, 90107, 90106, 90105, 90104, 90103, 90102, 90101, 90100]
    assert items[0].published == datetime(
        2027, 5, 21, 17, 5, tzinfo=IST
    )  # "Fri, 21 May 2027 17:05:00", no zone
    assert items[-1].url == URL.format(90100)  # lowercase "scripts" in the feed, the seed's form here
    assert items[2].body == "" and "Money Supply" in items[0].body  # 90107 has no text in the feed


def test_only_mpc_schedules_reschedules_and_resolutions_are_read():
    kinds = {i.prid: rbi.notice_kind(i.title) for i in rbi.parse_feed(FEED)}
    # minutes, the Governor's statement and "Scheduled Banks' Statement" are not MPC notices
    assert {p: k for p, k in kinds.items() if k} == {
        90100: "schedule",
        90102: "resolution",
        90106: "reschedule",
    }
    assert rbi.notice_kind("Unscheduled meeting of the Monetary Policy Committee") == "unscheduled"
    assert rbi.notice_kind("Schedule of Monetary Policy Committee Meetings for 2021-22") == "schedule"


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss version="2.0"><channel/></rss>',
        b"<html><body>Access denied</body></html>",
        b"",
        b'<rss version="2.0"><channel><title>PRESS RELEASES FROM RBI</title></channel></rss>',
    ],
)
def test_anything_but_a_plain_feed_with_items_is_refused(body):
    with pytest.raises(rbi.RbiError):
        rbi.parse_feed(body)


def test_the_schedule_gives_each_meetings_last_day_and_the_year_end():
    sched = rbi.parse_schedule(_item(90100).text)
    assert sched.fy == (2027, 2028)
    # independently: the last day of each listed meeting (the September one ends on 1 October)
    assert sched.days == [date(2027, 4, 7), date(2027, 6, 4), date(2027, 8, 5), date(2027, 10, 1), date(2027, 12, 8),
                          date(2028, 2, 9)]  # fmt: skip
    assert sched.through == date(2028, 3, 31)


def test_the_2026_27_schedule_form_reads_to_the_seeds_decision_days():
    # the real release (prid 62422, PR 2025-2026/2306 of 23-Mar-2026) is titled "... for 2026-2027" and lists
    # Apr 6-8, Jun 3-5, Aug 3-5, Oct 5-7, Dec 2-4 2026 and Feb 3-5 2027; its decision days are the seed's rows
    rows = "".join(f"<tr><td>{m}</td></tr>" for m in ("April 6, 7 and 8, 2026", "June 3, 4 and 5, 2026",
                   "August 3, 4 and 5, 2026", "October 5, 6 and 7, 2026", "December 2, 3 and 4, 2026",
                   "February 3, 4 and 5, 2027"))  # fmt: skip
    item = rbi.Item(prid=62422, published=None, title="Meeting Schedule of the Monetary Policy Committee for 2026-2027",
                    body=f"<p>The Monetary Policy Committee will meet on the dates below:</p><table>{rows}</table>"
                    "<p>Press Release: 2025-2026/2306</p>")  # fmt: skip
    sched = rbi.parse_schedule(item.text)
    assert sched.fy == (2026, 2027) and sched.through == date(2027, 3, 31)
    assert sched.days == [date(2026, 4, 8), date(2026, 6, 5), date(2026, 8, 5), date(2026, 10, 7), date(2026, 12, 4),
                          date(2027, 2, 5)]  # fmt: skip


def test_the_rescheduling_notice_names_the_old_and_new_last_days():
    # the cited release's own date ("dated March 24, 2027") and the holiday named in between are skipped
    move = rbi.parse_reschedule(_item(90106).text)
    assert (move.old, move.new) == (date(2027, 6, 4), date(2027, 6, 5))


def test_a_resolution_gives_the_meeting_held_and_the_next_one():
    res = rbi.parse_resolution(_item(90102))
    assert (res.held, res.next) == (date(2027, 4, 7), date(2027, 6, 4))
    cross = rbi.Item(prid=1, published=NOW, body="<p>The next meeting of the MPC is scheduled during December 3 to "
                     "5, 2025.</p>", title="Monetary Policy Statement, 2025-26 Resolution of the Monetary Policy "
                     "Committee (MPC) September 29 to October 1, 2025")  # fmt: skip
    assert (rbi.parse_resolution(cross).held, rbi.parse_resolution(cross).next) == (date(2025, 10, 1),
                                                                                    date(2025, 12, 5))  # fmt: skip
    silent = rbi.Item(prid=2, published=NOW, title=cross.title, body="")
    assert rbi.parse_resolution(silent).next is None  # no text yet: the held day still comes from the title


def test_an_incomplete_schedule_is_refused():
    short = _item(90100).text
    for row in ("April 5, 6 and 7, 2027", "June 2, 3 and 4, 2027", "August 3 – 5, 2027"):
        short = short.replace(row, "")
    with pytest.raises(rbi.RbiError):
        rbi.parse_schedule(short)


# ---- the nightly read: one GET of the feed through PoliteClient (respx; no network)


async def _no_sleep(_s: float) -> None:
    return None


def _http() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


def _known(*prids: int) -> refresh.Known:
    return refresh.Known(urls=frozenset(URL.format(p) for p in prids), covered={})


async def _read(feed: bytes = FEED, known: refresh.Known | None = None, now: datetime = NOW, status: int = 200,
                state: Path | None = None) -> refresh.Found:  # fmt: skip
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(rbi.FEED).mock(return_value=httpx.Response(status, content=feed))
        try:
            return await rbi.read(known or _known(), now, http=_http(), state_path=state)
        finally:
            assert [str(c.request.url) for c in mock.calls] == [
                rbi.FEED
            ]  # the feed only, never a release page


async def test_one_read_turns_the_feed_into_dated_facts(tmp_path):
    found = await _read(state=tmp_path / "rss.json")
    assert found.dataset == "rbi_rss" and found.urls == [rbi.FEED]
    got = {(e.day, e.source_url, e.title) for e in found.events}
    sched, held, moved = rbi.TITLE_SCHEDULED, TITLES["rbi_mpc"], rbi.TITLE_MOVED
    assert got == {
        # the 2027-28 schedule (90100): its meetings from today on (the April one has passed)
        (date(2027, 6, 4), URL.format(90100), sched), (date(2027, 8, 5), URL.format(90100), sched),
        (date(2027, 10, 1), URL.format(90100), sched), (date(2027, 12, 8), URL.format(90100), sched),
        (date(2028, 2, 9), URL.format(90100), sched),
        # the April resolution (90102): the meeting held, and the next one it names
        (date(2027, 4, 7), URL.format(90102), held), (date(2027, 6, 4), URL.format(90102), sched),
        # the rescheduling notice (90106): the new last day
        (date(2027, 6, 5), URL.format(90106), moved),
    }  # fmt: skip
    assert {(e.type, e.tier, e.scope, e.at) for e in found.events} == {("rbi_mpc", 1, "IN", None)}
    assert [(c.type, c.through, c.source_url) for c in found.coverage] == [
        ("rbi_mpc", date(2028, 3, 31), URL.format(90100))]  # fmt: skip
    assert found.moves == [refresh.Move("rbi_mpc", "IN", date(2027, 6, 4), date(2027, 6, 5))]
    # the resolution says no meeting comes between the one held and 4 June: a stale date in between is withdrawn
    assert found.replaces == [refresh.Replace("rbi_mpc", "IN", date(2027, 4, 7), date(2027, 6, 4))]
    assert found.notices == []


async def test_only_facts_and_links_are_kept_never_the_text(tmp_path):
    found = await _read(state=tmp_path / "rss.json")
    kept = repr((found.events, found.coverage, found.moves, found.replaces, found.notices))
    for text in ("45ZI", "repo rate", "public holiday", "Chief General Manager", "Press Release: 2027"):
        assert text not in kept


async def test_an_item_already_stored_is_not_read_again(tmp_path):
    found = await _read(known=_known(90100, 90102, 90106), state=tmp_path / "rss.json")
    assert (found.events, found.coverage, found.moves, found.replaces) == ([], [], [], [])


async def test_a_resolution_read_after_a_newer_notice_does_not_undo_it(tmp_path):
    # the April resolution (90102) reaches the reader only after May's rescheduling notice (90106) was stored, e.g.
    # because its text was not in the feed yet at the earlier read: its older next-meeting statement is not applied
    found = await _read(known=_known(90100, 90106), state=tmp_path / "rss.json")
    assert {(e.day, e.source_url) for e in found.events} == {(date(2027, 4, 7), URL.format(90102))}
    assert found.replaces == [] and found.moves == []


async def test_an_mpc_item_without_text_yet_is_a_notice_and_the_rest_is_read(tmp_path):
    # RBI's feed showed its newest item with an empty description for a while (23-Oct-2026)
    blank = FEED.replace(rbi.parse_feed(FEED)[-1].body.encode(), b"")
    found = await _read(feed=blank, state=tmp_path / "rss.json")
    assert found.coverage == [] and not any(e.source_url == URL.format(90100) for e in found.events)
    assert len(found.notices) == 1 and "90100" in found.notices[0] and "tier 1" in found.notices[0]
    assert {e.source_url for e in found.events} == {URL.format(90102), URL.format(90106)}


async def test_an_unscheduled_meeting_is_a_notice_and_adds_no_date(tmp_path):
    # off-cycle meetings have no fixed form (#478) and coverage is scheduled meetings only: the owner is told
    feed = FEED.replace("Governor’s Statement: April 7, 2027".encode(),
                        b"Unscheduled meeting of the Monetary Policy Committee")  # fmt: skip
    found = await _read(feed=feed, known=_known(90100, 90102, 90106), state=tmp_path / "rss.json")
    assert found.events == [] and len(found.notices) == 1
    assert "outside the schedule" in found.notices[0] and URL.format(90103) in found.notices[0]


async def test_a_refused_or_broken_feed_raises_and_keeps_no_state(tmp_path):
    state = tmp_path / "rss.json"
    with pytest.raises(rbi.RbiError):
        await _read(status=403, state=state)
    with pytest.raises(rbi.RbiError):
        await _read(feed=b"<html>Access denied</html>", state=state)
    unparsable = FEED.replace(b"April 5, 6 and 7, 2027", b"").replace(b"June 2, 3 and 4, 2027", b"").replace(
        b"August 3 &#8211; 5, 2027", b"")  # fmt: skip
    with pytest.raises(rbi.RbiError):
        await _read(feed=unparsable, state=state)
    assert not state.exists()


# ---- continuity: the feed lists only its newest ten releases, so a gap since the last read is reported


async def test_the_first_read_records_the_newest_release_seen(tmp_path):
    state = tmp_path / "rss.json"
    found = await _read(state=state)
    assert found.notices == []
    assert json.loads(state.read_text())["newest_prid"] == 90109


async def test_releases_that_left_the_feed_between_reads_are_reported(tmp_path):
    state = tmp_path / "rss.json"
    state.write_text(json.dumps({"newest_prid": 90095, "newest_published": "2027-03-22T17:00:00+05:30"}))
    found = await _read(known=_known(90100, 90102, 90106), state=state)
    # 90096-90099 were published after the last read and are no longer in the feed: never seen
    assert len(found.notices) == 1 and "4 release(s) (prid 90096-90099)" in found.notices[0]
    assert "2027-03-22 17:00" in found.notices[0] and "2027-03-24 17:00" in found.notices[0]
    assert json.loads(state.read_text())["newest_prid"] == 90109


async def test_no_gap_when_the_feed_still_overlaps_the_last_read(tmp_path):
    state = tmp_path / "rss.json"
    for last in (90099, 90104, 90109, 90120):  # contiguous, overlapping, unchanged, or ahead of this feed
        state.write_text(json.dumps({"newest_prid": last, "newest_published": "2027-05-17T17:00:00+05:30"}))
        found = await _read(known=_known(90100, 90102, 90106), state=state)
        assert found.notices == [], last
        assert json.loads(state.read_text())["newest_prid"] == max(last, 90109)  # never moves back


# ---- point in time once stored (refresh.apply): known from the first read, a moved date kept until the notice


@pytest.fixture
def clean(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE calendar_event, calendar_coverage"))
    yield env


async def test_stored_rows_are_known_from_the_read_and_moves_wait_for_the_notice(clean, tmp_path):
    from finresearch.db import session_scope
    from finresearch.eventcal import calendar as cal
    from finresearch.eventcal import store

    march = datetime(2027, 3, 24, 23, 59, tzinfo=IST)
    stale = datetime(2027, 3, 1, 23, 59, tzinfo=IST)
    with session_scope() as s:  # the 4-June meeting known since March, and a stale 3-June date
        store.upsert(s, [cal.Event(type="rbi_mpc", tier=1, scope="IN", day=d, at=None, title="t",
                                   source_url=URL.format(1), known_since=k) for d, k in
                         ((date(2027, 6, 4), march), (date(2027, 6, 3), stale))], [])  # fmt: skip
    found = await _read(state=tmp_path / "rss.json")
    with session_scope() as s:
        refresh.apply(s, found, NOW)
        ev, cov = store.load(s)
    rows = {e.day: (e.known_since, e.withdrawn_since) for e in ev}
    assert rows[date(2027, 6, 4)] == (march, NOW)  # first seen in March, withdrawn when the notice was read
    assert rows[date(2027, 6, 3)] == (stale, NOW)  # the resolution names 4 June as the next meeting
    assert rows[date(2027, 6, 5)] == (NOW, None) and rows[date(2027, 8, 5)] == (NOW, None)
    assert [(c.through, c.known_since) for c in cov] == [(date(2028, 3, 31), NOW)]
    win = {"start": date(2027, 6, 1), "end": date(2027, 6, 10)}
    full = [cal.Coverage(type=k, scope="IN", through=date(2028, 3, 31), source_url="x", known_since=march)
            for k in cal.TIER1_TYPES]  # fmt: skip
    before = datetime(2027, 5, 21, 20, 59, tzinfo=IST)
    assert [r["day"] for r in cal.window_check(ev, full, as_of=before, **win)["tier1"]] == ["2027-06-03",
                                                                                         "2027-06-04"]  # fmt: skip
    assert [r["day"] for r in cal.window_check(ev, full, as_of=NOW, **win)["tier1"]] == ["2027-06-05"]

"""#492: RBI policy-rate observations from RBI's RSS feed are stored append-only (refseries.policy_obs, table
policy_rate_obs): known from the first read, never overwritten, a conflicting later item stored and flagged, unknown
kept unknown, and nothing of an item's text kept. Test database only; the feed is the synthetic
tests/fixtures/eventcal/rbi_rss_rates.xml."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from finresearch.eventcal import rbi, refresh
from finresearch.fincalc.dates import IST
from finresearch.fincalc.policy_feed import Observation

URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid={}"
DEC = date(2026, 12, 4)
TITLE = "Monetary Policy Statement, 2026-27 Resolution of the Monetary Policy Committee December 2 to 4, 2026"
T1 = datetime(2026, 12, 4, 12, 0, tzinfo=IST)  # the 12:00 read on the decision day
T2 = datetime(2026, 12, 4, 16, 30, tzinfo=IST)  # the 16:30 read


def obs(prid: int, decided: date, repo: str | None, sdf: str | None = None, msf: str | None = None,
        title: str = TITLE) -> Observation:  # fmt: skip
    def d(x: str | None) -> Decimal | None:
        return None if x is None else Decimal(x)

    return Observation(
        decided=decided, repo=d(repo), sdf=d(sdf), msf=d(msf), title=title, url=URL.format(prid)
    )


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE policy_rate_obs, calendar_event, calendar_coverage"))
    yield env


def _apply(now: datetime, *observations: Observation) -> dict:
    from finresearch.db import session_scope

    with session_scope() as s:
        return refresh.apply(s, refresh.Found("rbi_rss", observations=list(observations)), now)


def _stored() -> list[Observation]:
    from finresearch.db import session_scope
    from finresearch.refseries import policy_obs

    with session_scope() as s:
        return policy_obs.load(s)


def test_each_observation_is_stored_once_known_from_its_first_read(clean):
    hike = obs(91004, DEC, "0.0575", "0.055", "0.06")
    assert _apply(T1, hike)["policy_rates"] == 1
    assert _apply(T2, hike)["policy_rates"] == 0  # the 16:30 read sees the same item: nothing new
    assert [(o.url, o.decided, o.seen, o.repo, o.sdf, o.msf, o.conflict) for o in _stored()] == [
        (URL.format(91004), DEC, T1, Decimal("0.0575"), Decimal("0.055"), Decimal("0.06"), None)]  # fmt: skip


def test_a_later_item_with_other_rates_is_stored_and_flagged_never_merged(clean):
    _apply(T1, obs(91004, DEC, "0.0575", "0.055", "0.06"))
    _apply(T2, obs(91012, DEC, "0.06", "0.0575", "0.0625", title=f"{TITLE} (revised)"))
    first, later = _stored()
    assert (first.url, first.seen, first.repo, first.conflict) == (
        URL.format(91004),
        T1,
        Decimal("0.0575"),
        None,
    )
    assert (later.url, later.seen, later.repo) == (URL.format(91012), T2, Decimal("0.06"))
    # the flag names the earlier item and both figures: repo 6.00 % here against 5.75 % there
    assert URL.format(91004) in later.conflict and "6.00 %" in later.conflict and "5.75 %" in later.conflict


def test_the_same_item_read_again_with_other_figures_is_a_new_flagged_row(clean):
    _apply(T1, obs(91004, DEC, "0.0575", "0.055", "0.06"))
    _apply(T2, obs(91004, DEC, "0.0575", "0.0525", "0.06"))  # the SDF figure changed between reads
    first, later = _stored()
    assert (first.sdf, first.conflict, later.sdf) == (Decimal("0.055"), None, Decimal("0.0525"))
    assert "SDF" in later.conflict and "5.25 %" in later.conflict and "5.50 %" in later.conflict


def test_a_fuller_statement_of_the_same_rates_is_no_conflict(clean):
    _apply(T1, obs(91004, DEC, "0.0575"))  # the repo rate only
    _apply(T2, obs(91012, DEC, "0.0575", "0.055", "0.06"))
    assert [(o.url, o.conflict) for o in _stored()] == [(URL.format(91004), None), (URL.format(91012), None)]


def test_unknown_rates_are_stored_as_unknown_never_zero(clean):
    from finresearch.db import session_scope

    _apply(T1, obs(91009, date(2027, 6, 4), None))
    with session_scope() as s:
        assert tuple(s.execute(text("SELECT repo, sdf, msf FROM policy_rate_obs")).one()) == (
            None,
            None,
            None,
        )


async def _no_sleep(_s: float) -> None:
    return None


async def test_a_read_of_the_feed_keeps_numbers_dates_link_and_title_only(clean, tmp_path):
    from finresearch.adapters.http import PoliteClient
    from finresearch.db import session_scope

    feed = (Path(__file__).parent / "fixtures" / "eventcal" / "rbi_rss_rates.xml").read_bytes()
    now = datetime(2027, 8, 5, 12, 0, tzinfo=IST)
    http = PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(rbi.FEED).mock(return_value=httpx.Response(200, content=feed))
        found = await rbi.read(refresh.Known(urls=frozenset(), covered={}), now, http=http,
                               state_path=tmp_path / "rss.json")  # fmt: skip
    with session_scope() as s:
        assert refresh.apply(s, found, now)["policy_rates"] == 5  # 91001, 91004, 91006, 91008 and 91009
        rows = [dict(r) for r in s.execute(text("SELECT * FROM policy_rate_obs ORDER BY decided")).mappings()]
    assert [r["url"] for r in rows] == [URL.format(p) for p in (91001, 91004, 91006, 91008, 91009)]
    kept = repr(rows)
    for phrase in (
        "voted",
        "liquidity adjustment facility",
        "Consequently",
        "Chief General Manager",
        "Press Release",
    ):
        assert phrase not in kept


# ---- the risk-free chain reads the stored observations and the event calendar's MPC schedule (signals.rates)


def _calendar(*days: tuple[date, bool], through: date = date(2027, 3, 31)) -> None:
    """rbi_mpc meetings (day, standing) and the 2026-27 schedule's coverage, as the calendar's refresh stores them."""
    from finresearch.db import session_scope
    from finresearch.eventcal import calendar as cal
    from finresearch.eventcal import store

    march = datetime(2026, 3, 23, 21, 0, tzinfo=IST)
    events = [cal.Event(type="rbi_mpc", tier=1, scope="IN", day=d, at=None, title="t", source_url=URL.format(62422),
                        known_since=march, withdrawn_since=None if standing else T1) for d, standing in days]  # fmt: skip
    with session_scope() as s:
        store.upsert(s, events, [cal.Coverage(type="rbi_mpc", scope="IN", through=through,
                                              source_url=URL.format(62422), known_since=march)])  # fmt: skip


def _rate(day: date) -> dict:
    from finresearch.db import session_scope
    from finresearch.signals import rates

    with session_scope() as s:
        return rates.rate_on(s, day, 91 / 365)


def test_the_chain_reads_the_stored_decision_and_clears_the_flag(clean, monkeypatch):
    from finresearch.signals import rates

    monkeypatch.setattr(rates, "OBSERVED", None)  # read policy_rate_obs and the calendar in the test database
    _calendar((date(2026, 12, 4), True), (date(2027, 2, 5), True))
    unread = _rate(date(2026, 12, 10))
    assert (
        unread["stale"] is True
        and unread["yield"] == pytest.approx(0.055)
        and "04-Dec-2026" in unread["note"]
    )
    _apply(T1, obs(91004, DEC, "0.0575", "0.055", "0.06"))
    read = _rate(date(2026, 12, 10))
    assert read["stale"] is False and read["yield"] == pytest.approx(0.0575) and read["as_of"] == "2026-12-04"
    assert "from RBI's RSS feed" in read["source"] and "seen 04-Dec-2026 12:00 IST" in read["source"]


def test_a_withdrawn_meeting_date_is_not_waited_for(clean, monkeypatch):
    from finresearch.signals import rates

    monkeypatch.setattr(rates, "OBSERVED", None)
    _calendar(
        (date(2026, 12, 4), False), (date(2026, 12, 5), True), (date(2027, 2, 5), True)
    )  # moved to 5 Dec
    _apply(T1, obs(91004, date(2026, 12, 5), "0.0575", "0.055", "0.06"))
    assert _rate(date(2026, 12, 10))["stale"] is False


def test_unreadable_observations_leave_the_table_alone_and_say_so(clean, monkeypatch):
    from finresearch.refseries import policy_obs
    from finresearch.signals import rates

    def broken(_s):
        raise RuntimeError("synthetic read failure")

    monkeypatch.setattr(rates, "OBSERVED", None)
    monkeypatch.setattr(policy_obs, "observed", broken)
    got = _rate(date(2026, 12, 10))
    assert got["yield"] == pytest.approx(0.055) and got["stale"] is True  # never "fresh" without the feed
    assert "could not be read" in got["note"]


def test_the_factsheet_rate_uses_a_decision_read_from_the_feed(clean, monkeypatch):
    from finresearch.signals import rates

    monkeypatch.setattr(rates, "OBSERVED", None)
    _calendar((date(2026, 12, 4), True), (date(2027, 2, 5), True))
    _apply(T1, obs(91004, DEC, "0.0575", "0.055", "0.06"))
    fs = rates.factsheet(date(2026, 12, 31))
    assert fs["rate"] == pytest.approx(0.0575) and fs["url"] == URL.format(91004)
    assert "RBI's RSS feed" in fs["source"]

"""#492: RBI's policy rates read from the MPC resolutions in RBI's press-release RSS feed, which eventcal.rbi already
fetches for the event calendar (#481): no other request, and only the parsed numbers, dates, link and title are kept.
The feed is tests/fixtures/eventcal/rbi_rss_rates.xml (synthetic items, real wording). Expected rates are the figures
the items print, written as literal fractions (5.75 per cent = 0.0575), never computed by the code under test."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import respx

from finresearch.adapters.http import PoliteClient
from finresearch.eventcal import rbi, refresh
from finresearch.fincalc.dates import IST

FEED = (Path(__file__).parent / "fixtures" / "eventcal" / "rbi_rss_rates.xml").read_bytes()
URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid={}"
NOW = datetime(2027, 8, 5, 12, 0, tzinfo=IST)  # the 12:00 read on the day of the newest item
DEC = "Monetary Policy Statement, 2026-27 Resolution of the Monetary Policy Committee December 2 to 4, 2026"


def _item(prid: int) -> rbi.Item:
    return next(i for i in rbi.parse_feed(FEED) if i.prid == prid)


def _rates(o) -> tuple:
    return o.decided, o.repo, o.sdf, o.msf


def test_a_hike_gives_the_decision_day_and_all_three_rates():
    # "Increase the policy repo rate ... by 25 bps to 5.75 per cent"; "the standing deposit facility (SDF) rate stands
    # adjusted at 5.50 per cent and the marginal standing facility (MSF) rate and the Bank Rate at 6.00 per cent"
    o = rbi.observe(_item(91004))
    assert _rates(o) == (date(2026, 12, 4), Decimal("0.0575"), Decimal("0.055"), Decimal("0.06"))
    assert (o.url, o.title) == (URL.format(91004), DEC)
    assert o.published == datetime(2026, 12, 4, 10, 0, tzinfo=IST)


def test_a_cut_reads_the_decision_not_the_old_rate_a_recap_or_a_dissent():
    # "to 5.50 per cent from 5.75 per cent"; later the text recalls December's 5.75 % and one member "voted to keep
    # the policy repo rate unchanged at 5.75 per cent": the decision is the first statement
    o = rbi.observe(_item(91006))
    assert _rates(o) == (date(2027, 2, 5), Decimal("0.055"), Decimal("0.0525"), Decimal("0.0575"))


def test_a_hold_reads_the_rates_kept_unchanged():
    # "keep the policy repo rate ... unchanged at 5.25 per cent"; "the SDF rate remains at 5.00 per cent"
    o = rbi.observe(_item(91001))
    assert _rates(o) == (date(2026, 8, 5), Decimal("0.0525"), Decimal("0.05"), Decimal("0.055"))


def test_a_resolution_stating_only_the_repo_rate_leaves_sdf_and_msf_unknown():
    o = rbi.observe(_item(91008))
    assert _rates(o) == (date(2027, 4, 7), Decimal("0.055"), None, None)


def test_a_resolution_with_no_figure_is_unknown_never_zero():
    o = rbi.observe(_item(91009))  # "decided to keep the policy repo rate unchanged."
    assert _rates(o) == (date(2027, 6, 4), None, None, None)


def test_an_empty_description_is_not_read_yet():
    # RBI's feed shows a new item with an empty description for a few minutes (23-Oct-2026): read on a later pass
    assert rbi.observe(_item(91010)) is None


def test_minutes_and_other_releases_quoting_a_repo_rate_are_not_read():
    assert rbi.observe(_item(91007)) is None  # minutes of the February meeting
    assert rbi.observe(_item(91005)) is None  # money market operations


def _resolution(body: str) -> rbi.Item:
    return rbi.Item(prid=91100, published=None, body=body, title=DEC)


def test_percent_signs_and_basis_points_are_read():
    o = rbi.observe(_resolution("<p>The MPC decided to raise the policy repo rate by 50 basis points to 6.25%. "
                                "The SDF rate stands adjusted to 6.00% and the MSF rate to 6.50%.</p>"))  # fmt: skip
    assert _rates(o)[1:] == (Decimal("0.0625"), Decimal("0.06"), Decimal("0.065"))


def test_other_wordings_of_a_hold_are_read():
    for verb in ("retain", "maintain", "leave"):
        o = rbi.observe(_resolution(f"<p>The MPC decided to {verb} the policy repo rate under the liquidity "
                                    "adjustment facility (LAF) unchanged at 5.50 per cent.</p>"))  # fmt: skip
        assert o.repo == Decimal("0.055"), verb


def test_a_recap_before_the_decision_is_not_read_as_the_decision():
    # only "keep/increase/raise/reduce/cut/lower/hike the policy repo rate" states a decision: a recap of the last one
    # ("raised the policy repo rate ... to 5.50 per cent") placed first must not be taken for this meeting's 5.75 %
    o = rbi.observe(_resolution("<p>Since the MPC raised the policy repo rate by 25 bps to 5.50 per cent in October, "
                                "inflation has risen.</p><p>The MPC decided to increase the policy repo rate by 25 "
                                "bps to 5.75 per cent.</p>"))  # fmt: skip
    assert o.repo == Decimal("0.0575")


def test_rates_that_break_the_corridor_are_unknown():
    # an SDF above the repo rate cannot be RBI's corridor (SDF < repo < MSF): which figure is wrong is unknown
    o = rbi.observe(_resolution("<p>The MPC decided to keep the policy repo rate unchanged at 5.50 per cent. The "
                                "SDF rate remains at 5.75 per cent and the MSF rate at 6.00 per cent.</p>"))  # fmt: skip
    assert _rates(o)[1:] == (None, None, None)
    o = rbi.observe(
        _resolution("<p>The MPC decided to keep the policy repo rate unchanged at 55.0 per cent.</p>")
    )
    assert o.repo is None  # outside any plausible policy rate (0 to 20 %)


def test_an_observation_keeps_numbers_dates_link_and_title_never_the_text():
    kept = repr(rbi.observe(_item(91004)))
    for text in (
        "voted",
        "liquidity adjustment facility",
        "Consequently",
        "Chief General Manager",
        "Press Release",
    ):
        assert text not in kept


# ---- the read: the same single GET of the feed (eventcal.rbi.read), observations beside the calendar's facts


async def _no_sleep(_s: float) -> None:
    return None


async def _read(known: refresh.Known, tmp_path: Path) -> refresh.Found:
    http = PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(rbi.FEED).mock(return_value=httpx.Response(200, content=FEED))
        found = await rbi.read(known, NOW, http=http, state_path=tmp_path / "rss.json")
        assert [str(c.request.url) for c in mock.calls] == [rbi.FEED]  # the feed only, never a release page
    return found


async def test_one_read_observes_every_resolution_with_text(tmp_path):
    found = await _read(refresh.Known(urls=frozenset(), covered={}), tmp_path)
    got = {o.url: _rates(o) for o in found.observations}
    assert got == {
        URL.format(91001): (date(2026, 8, 5), Decimal("0.0525"), Decimal("0.05"), Decimal("0.055")),
        URL.format(91004): (date(2026, 12, 4), Decimal("0.0575"), Decimal("0.055"), Decimal("0.06")),
        URL.format(91006): (date(2027, 2, 5), Decimal("0.055"), Decimal("0.0525"), Decimal("0.0575")),
        URL.format(91008): (date(2027, 4, 7), Decimal("0.055"), None, None),
        URL.format(91009): (date(2027, 6, 4), None, None, None),  # seen, rates unknown
    }  # 91010 (empty) is read on a later pass; 91005 and 91007 are not resolutions


async def test_a_resolution_already_in_the_calendar_is_still_observed(tmp_path):
    # the calendar skips an item whose dates it stored; its rates are read all the same (the store keeps the first
    # sighting), so a resolution stored before #492 or by an earlier pass is not lost
    known = refresh.Known(
        urls=frozenset(URL.format(p) for p in (91001, 91004, 91006, 91008, 91009)), covered={}
    )
    found = await _read(known, tmp_path)
    assert found.events == [] and {o.url for o in found.observations} == {
        URL.format(p) for p in (91001, 91004, 91006, 91008, 91009)}  # fmt: skip

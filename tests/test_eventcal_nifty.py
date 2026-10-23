"""#367 refresh: NSE index rebalances from NSE Indices' press-release listing (a hand-made copy of
www.niftyindices.com/press-release in tests/fixtures/eventcal; synthetic titles and dates)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.http import PoliteClient
from finresearch.eventcal import calendar as cal
from finresearch.eventcal import nifty_indices as ni
from finresearch.eventcal import refresh
from finresearch.fincalc.dates import IST

PAGE = (Path(__file__).parent / "fixtures" / "eventcal" / "nifty_press.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 23, 21, 0, tzinfo=IST)  # a Friday
KNOWN = refresh.Known(urls=frozenset(), covered={})


def test_listing_reads_every_press_item_in_page_order():
    items = ni.parse_listing(PAGE)
    assert len(items) == 8
    first = items[0]
    assert first.published == date(2026, 10, 21)
    assert first.url == "https://www.niftyindices.com/Press_Release/ind_prs21102026.pdf"
    assert first.title == "Replacement in Nifty India FPI 150 index w.e.f. November 2, 2026"


def test_only_constituent_changes_of_equity_indices_are_candidates():
    kept = [i.title for i in ni.parse_listing(PAGE) if ni.is_candidate(i.title)]
    assert kept == ["Replacement in Nifty India FPI 150 index w.e.f. November 2, 2026",
                    "Replacements in indices w.e.f. November 30, 2026",
                    "Replacements in indices w.e.f. September 30, 2026",
                    "Change in index w.e.f. (date in the release)"]  # fmt: skip


async def _no_sleep(_s: float) -> None:
    return None


def _http() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


async def _read(
    page: str = PAGE, holidays: set[date] | None = frozenset(), now: datetime = NOW
) -> refresh.Found:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(ni.PAGE).mock(return_value=httpx.Response(200, text=page))
        return await ni.read(
            KNOWN, now, http=_http(), holidays_for=lambda d: None if holidays is None else set(holidays)
        )


async def test_read_dates_each_future_rebalance_on_the_session_before_it_takes_effect():
    found = await _read()
    # independently: w.e.f. Mon 2-Nov -> Fri 30-Oct; w.e.f. Mon 30-Nov -> Fri 27-Nov (no holidays)
    assert sorted(e.day for e in found.events) == [date(2026, 10, 30), date(2026, 11, 27)]
    assert {(e.type, e.tier, e.scope, e.at) for e in found.events} == {("index_rebalance", 2, "IN", None)}
    assert {e.source_url for e in found.events} == {
        "https://www.niftyindices.com/Press_Release/ind_prs21102026.pdf",
        "https://www.niftyindices.com/Press_Release/ind_prs20102026.pdf"}  # fmt: skip
    assert (
        found.coverage == [] and found.replaces == [] and found.moves == []
    )  # context: never "no rebalance"
    assert all("w.e.f." in e.title and "approximate" not in e.title for e in found.events)


async def test_a_holiday_before_the_effective_date_moves_the_flow_session_earlier():
    found = await _read(holidays={date(2026, 11, 27)})
    assert date(2026, 11, 26) in {e.day for e in found.events}


async def test_an_unknown_holiday_year_falls_back_to_the_previous_weekday_and_says_so():
    found = await _read(holidays=None)
    assert sorted(e.day for e in found.events) == [date(2026, 10, 30), date(2026, 11, 27)]
    assert all("approximate" in e.title for e in found.events)


async def test_a_recent_candidate_without_an_effective_date_is_reported_never_guessed():
    # 3-5 real titles a year carry no date ("Replacements in indices", 2024-09-25): a notice to read the release,
    # no row, and the source keeps working
    bare = PAGE.replace("Replacements in indices w.e.f. November 30, 2026", "Replacements in indices")
    found = await _read(page=bare)
    assert [e.day for e in found.events] == [date(2026, 10, 30)]
    assert len(found.notices) == 1 and "ind_prs20102026.pdf" in found.notices[0]
    assert "no effective date" in found.notices[0]


def test_effective_date_with_or_without_the_comma_before_the_year():
    # both forms are on the real page ("w.e.f. July 01 2026", 2026-06-24)
    assert ni.effective_date("Replacement in Nifty Smallcap 500 w.e.f. July 01 2026") == date(2026, 7, 1)
    assert ni.effective_date("Replacement in indices w.e.f. October 13, 2026") == date(2026, 10, 13)
    assert ni.effective_date("Replacements in indices w.e.f. Sept 30, 2026") == date(2026, 9, 30)
    assert ni.effective_date("Replacements in indices") is None


async def test_a_page_without_press_items_is_refused():
    with pytest.raises(ni.NiftyIndicesError):
        await _read(page="<html><body>Access Denied</body></html>")


def test_index_rebalance_is_a_titled_context_type():
    assert "index_rebalance" in cal.TITLES and "index_rebalance" not in cal.TIER1_TYPES

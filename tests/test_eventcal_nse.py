"""#367 refresh: NSE's F&O circulars, read only to report expiry-day revision notices for the owner to review (a
synthetic copy of the /api/circulars answer in tests/fixtures/eventcal; NseClient warm-up and API mocked with respx)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NseClient
from finresearch.eventcal import nse, refresh
from finresearch.fincalc.dates import IST

LISTING = json.loads((Path(__file__).parent / "fixtures" / "eventcal" / "nse_circulars.json").read_text())
NOW = datetime(2026, 10, 23, 21, 0, tzinfo=IST)
KNOWN = refresh.Known(urls=frozenset(), covered={})
API = "https://www.nseindia.com/api/circulars"


async def _no_sleep(_s: float) -> None:
    return None


def _client() -> NseClient:
    return NseClient(PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0),
                     warmup_url=nse.PAGE)  # fmt: skip


def test_only_fo_expiry_day_revisions_are_notices():
    got = nse.expiry_notices(LISTING)
    assert [n.split(" ")[0] for n in got] == ["NSE/FAOP/99003", "NSE/FAOP/99000"]
    assert "Revision in Expiry Day of Index Derivatives Contracts" in got[0]
    assert "https://nsearchives.nseindia.com/content/circulars/FAOP99003.pdf" in got[0]


async def _read(known: refresh.Known = KNOWN) -> tuple[refresh.Found, respx.Route, list[str]]:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(nse.PAGE).mock(return_value=httpx.Response(200, text="<html/>"))
        api = mock.get(API).mock(return_value=httpx.Response(200, json=LISTING))
        found = await nse.read(known, NOW, nse=_client())
    return found, api, [str(c.request.url) for c in mock.calls]


async def test_read_asks_one_window_and_downloads_no_circular():
    # since #481 the index-change circular (99002) is kept as a dated fact; before, nothing was stored
    found, api, urls = await _read()
    assert api.call_count == 1
    params = api.calls[0].request.url.params
    assert (params["fromDate"], params["toDate"]) == ("13-10-2026", "23-10-2026")  # LOOKBACK_DAYS = 10
    assert len(found.notices) == 2
    assert [e.day for e in found.events] == [date(2026, 10, 21)]
    assert found.coverage == [] and found.moves == [] and found.replaces == []
    assert not any("nsearchives" in u for u in urls)  # no circular body is downloaded


CIRCULAR = "https://nsearchives.nseindia.com/content/circulars/FAOP99002.pdf"
WINDOW = (
    "NSE index constituent change: NIFTY semi-annual review, expected window (effective 22-Oct-2026, ...)"
)


def _expecting(day: date) -> refresh.Known:
    return refresh.Known(urls=frozenset(), covered={}, standing={"index_rebalance": {day: WINDOW}})


async def test_an_index_change_circular_confirms_the_expected_window():
    found, _, _ = await _read(_expecting(date(2026, 10, 21)))
    [e] = found.events
    assert (e.type, e.tier, e.scope, e.day, e.source_url) == (
        "index_rebalance",
        2,
        "IN",
        date(2026, 10, 21),
        CIRCULAR,
    )
    assert "confirmed" in e.title and "NSE/FAOP/99002" in e.title and "expected" not in e.title
    assert found.moves == []


async def test_a_circular_on_another_day_moves_the_expected_window():
    found, _, _ = await _read(_expecting(date(2026, 10, 19)))
    assert [e.day for e in found.events] == [date(2026, 10, 21)]
    assert found.moves == [refresh.Move("index_rebalance", "IN", date(2026, 10, 19), date(2026, 10, 21))]


async def test_a_circular_far_from_any_window_is_an_unscheduled_change():
    found, _, _ = await _read(_expecting(date(2027, 3, 30)))
    [e] = found.events
    assert e.day == date(2026, 10, 21) and "NSE/FAOP/99002" in e.title and "confirmed" not in e.title
    assert found.moves == []


async def test_a_circular_already_stored_is_not_read_again():
    found, _, _ = await _read(refresh.Known(urls=frozenset({CIRCULAR}), covered={}))
    assert found.events == [] and len(found.notices) == 2


async def test_an_answer_without_its_list_is_an_error():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(nse.PAGE).mock(return_value=httpx.Response(200, text="<html/>"))
        mock.get(API).mock(return_value=httpx.Response(200, json={"error": "blocked"}))
        with pytest.raises(nse.NseCircularsError):
            await nse.read(KNOWN, NOW, nse=_client())

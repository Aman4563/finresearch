"""#366: FBIL's T-bill and MIBOR fixings (JSON) and the G-sec published-dates list. Synthetic payloads in the
layout FBIL's site reads (wasdm/<series>/fetchfiltered); the rates are made up."""

from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from finresearch.adapters import fbil
from finresearch.adapters.http import PoliteClient

TBILL = [
    {"processRunDate": "2022-01-03 00:00:00", "displayTime": "2022-01-03 17:30:00", "tenorName": "7 Days",
     "rate": 3.41, "comments": " "},
    {"processRunDate": "2022-01-03 00:00:00", "displayTime": "2022-01-03 17:30:00", "tenorName": "3 Months",
     "rate": 3.62, "comments": " "},
    {"processRunDate": "2022-01-03 00:00:00", "displayTime": "2022-01-03 17:30:00", "tenorName": "12 Months",
     "rate": 4.5, "comments": " "},
    {"processRunDate": "2022-01-03 00:00:00", "displayTime": "2022-01-03 17:30:00", "tenorName": "6 Months",
     "rate": None, "comments": "not computed"},
]  # fmt: skip
OVN = [{"processRunDate": "2015-07-24 00:00:00", "tenor": "3D", "displayTime": "2015-07-24 10:45:00", "rate": 7.1},
       {"processRunDate": "2015-07-23 00:00:00", "tenor": "O/N", "displayTime": "2015-07-23 10:45:00", "rate": 7.2}]  # fmt: skip
TERM = [{"processRunDate": "2026-10-14 00:00:00", "tenor": "14 DAYS", "displayTime": "2026-10-14 13:45:00", "rate": 5.8},
        {"processRunDate": "2026-10-14 00:00:00", "tenor": "1 MONTH", "displayTime": "2026-10-14 13:45:00", "rate": 5.9}]  # fmt: skip


def test_tbill_fixings_parse_tenors_and_fractions():
    rows = fbil.parse_fixings("tbill", TBILL)
    assert [(r.tenor, r.tenor_days) for r in rows] == [("7D", 7), ("3M", 91), ("12M", 364)]
    assert rows[1].day == date(2022, 1, 3) and rows[1].rate == Decimal("0.0362")
    assert rows[1].published_at == datetime(2022, 1, 3, 17, 30)


def test_a_fixing_without_a_rate_is_left_out_never_zero():
    assert all(r.tenor != "6M" for r in fbil.parse_fixings("tbill", TBILL))


def test_mibor_tenors():
    assert [(r.tenor, r.tenor_days) for r in fbil.parse_fixings("mibor_on", OVN)] == [("3D", 3), ("ON", 1)]
    assert [(r.tenor, r.tenor_days) for r in fbil.parse_fixings("mibor_term", TERM)] == [
        ("14D", 14),
        ("1M", 30),
    ]


def test_unknown_tenor_label_is_refused():
    with pytest.raises(fbil.FbilError):
        fbil.parse_fixings("tbill", [dict(TBILL[0], tenorName="Fortnight-ish")])


def test_a_non_list_payload_is_refused():
    with pytest.raises(fbil.FbilError):
        fbil.parse_fixings("tbill", {"error": "blocked"})


def _client():
    return fbil.FbilClient(PoliteClient(cache_dir=None, clock=lambda: 0.0))


@respx.mock
async def test_client_fetches_one_series_for_a_window():
    route = respx.get(f"{fbil.BASE}/tbill/fetchfiltered").mock(return_value=httpx.Response(200, json=TBILL))
    async with _client() as c:
        url, body, rows = await c.fixings("tbill", date(2022, 1, 1), date(2022, 1, 31))
    q = route.calls.last.request.url.params
    assert (q["fromDate"], q["toDate"], q["authenticated"]) == ("2022-01-01", "2022-01-31", "false")
    assert len(rows) == 3 and body.startswith(b"[") and "tbill/fetchfiltered" in url


@respx.mock
async def test_client_raises_on_an_html_answer():
    respx.get(f"{fbil.BASE}/ovnmibor/fetchfiltered").mock(
        return_value=httpx.Response(200, text="<html>no</html>")
    )
    async with _client() as c:
        with pytest.raises(fbil.FbilError):
            await c.fixings("mibor_on", date(2022, 1, 1), date(2022, 1, 31))


@respx.mock
async def test_gsec_published_dates_in_a_window():
    respx.get(f"{fbil.BASE}/gsec/fetchfiltered").mock(return_value=httpx.Response(200, json=[
        {"processRunDate": "2018-04-03", "archive": "FBIL GOI Prices including Par Yield"},
        {"processRunDate": "2018-03-31", "archive": "FBIL GOI Prices including Par Yield"}]))  # fmt: skip
    async with _client() as c:
        assert await c.gsec_dates(date(2018, 3, 1), date(2018, 4, 30)) == [
            date(2018, 3, 31),
            date(2018, 4, 3),
        ]

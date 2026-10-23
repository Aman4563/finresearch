"""#367 refresh: India CPI and GDP release dates from MoSPI's Advance Release Calendar (a hand-made pdftotext -layout
copy in tests/fixtures/eventcal; the PDF step is a seam, so no test shells out)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters.http import PoliteClient
from finresearch.eventcal import mospi, refresh
from finresearch.fincalc.dates import IST

FIX = Path(__file__).parent / "fixtures" / "eventcal"
ARC = (FIX / "mospi_arc_layout.txt").read_text(encoding="utf-8")
PDF_URL = "https://www.mospi.gov.in/uploads/documents/releaseCalender/1790000000000-ARC_2026_27_test.pdf"
NOW = datetime(2026, 10, 23, 21, 0, tzinfo=IST)
# independently, from the fixture's rows: CPI on the 12th of every month, GDP on five days
CPI = [date(2026, m, 12) for m in range(4, 13)] + [date(2027, m, 12) for m in (1, 2, 3)]
GDP = [date(2026, 5, 29), date(2026, 8, 31), date(2026, 11, 30), date(2027, 1, 7), date(2027, 2, 26)]


def test_parse_reads_every_cpi_and_gdp_date_and_nothing_else():
    got = mospi.parse_arc(ARC)
    assert got.fy == (2026, 2027) and got.through == date(2027, 3, 31)
    assert got.days["india_cpi"] == CPI
    assert got.days["india_gdp"] == GDP  # not the IIP row whose link names GDP, nor the ISP or PLFS rows


def test_a_calendar_missing_a_cpi_month_is_refused():
    with pytest.raises(mospi.MospiCalendarError):
        mospi.parse_arc(
            ARC.replace("12th Sep   All India Consumer Price Index", "12th Sep   All India Index")
        )


def test_a_calendar_without_its_year_is_refused():
    with pytest.raises(mospi.MospiCalendarError):
        mospi.parse_arc(ARC.replace("ADVANCE RELEASE CALENDAR (2026-27)", "RELEASE CALENDAR"))


async def _no_sleep(_s: float) -> None:
    return None


def _http() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


def _known(*urls: str) -> refresh.Known:
    return refresh.Known(urls=frozenset(urls), covered={})


def _mock(mock, pdf: bytes = b"%PDF-1.4 synthetic"):
    mock.get(mospi.LATEST).mock(
        return_value=httpx.Response(200, text=(FIX / "mospi_arc_latest.json").read_text())
    )
    return mock.get(PDF_URL).mock(return_value=httpx.Response(200, content=pdf))


async def test_read_keeps_future_dates_with_coverage_through_the_year_end():
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock)
        found = await mospi.read(_known(), NOW, http=_http(), to_text=lambda b: ARC)
    got = {(e.type, e.day) for e in found.events}
    assert got == {("india_cpi", d) for d in CPI if d >= NOW.date()} | {
        ("india_gdp", d) for d in GDP if d >= NOW.date()
    }
    assert {(e.tier, e.scope, e.source_url, e.at) for e in found.events} == {(2, "IN", PDF_URL, None)}
    assert sorted((c.type, c.through, c.source_url) for c in found.coverage) == [
        ("india_cpi", date(2027, 3, 31), PDF_URL), ("india_gdp", date(2027, 3, 31), PDF_URL)]  # fmt: skip
    assert found.replaces == [refresh.Replace("india_cpi", "IN", date(2026, 10, 22), date(2027, 3, 31)),
                                      refresh.Replace("india_gdp", "IN", date(2026, 10, 22), date(2027, 3, 31))]  # fmt: skip


async def test_read_skips_a_calendar_already_stored():
    with respx.mock(assert_all_called=False) as mock:
        pdf = _mock(mock)
        found = await mospi.read(_known(PDF_URL), NOW, http=_http(), to_text=lambda b: ARC)
    assert not pdf.called and found.events == [] and found.coverage == [] and found.replaces == []


async def test_a_refused_pointer_or_a_body_that_is_not_a_pdf_is_an_error():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(mospi.LATEST).mock(return_value=httpx.Response(200, json={"status": "error", "data": None}))
        with pytest.raises(mospi.MospiCalendarError):
            await mospi.read(_known(), NOW, http=_http(), to_text=lambda b: ARC)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, pdf=b"<html>blocked</html>")
        with pytest.raises(mospi.MospiCalendarError):
            await mospi.read(_known(), NOW, http=_http(), to_text=lambda b: ARC)


async def test_next_years_calendar_never_withdraws_this_years_remaining_dates():
    # the 2027-28 calendar read on 2-Mar-2027 does not list 12-Mar-2027 (2026-27's): its replacement starts 1-Apr-2027
    nxt = ARC.replace("(2026-27)", "(2027-28)")
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock)
        found = await mospi.read(
            _known(), datetime(2027, 3, 2, 21, 0, tzinfo=IST), http=_http(), to_text=lambda b: nxt
        )
    assert {(r.after, r.through) for r in found.replaces} == {(date(2027, 3, 31), date(2028, 3, 31))}

"""NSE F&O bhavcopy parsing and fetching (#306): both formats, units, the tri-state fetch. Fixtures are rows cut from
NSE's public files of 30-Sep-2026 (UDiFF) and 05-Jul-2024 (legacy); no network."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters import nse_fno_bhav as fb
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NseError

FX = Path(__file__).parent / "fixtures" / "fno_bhav"
UDIFF = (FX / "udiff_20260930_sample.csv").read_text()
LEGACY = (FX / "legacy_fo05JUL2024_sample.csv").read_text()


def zipped(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def test_udiff_future_and_option_fields():
    rows, total = fb.parse_udiff(UDIFF)
    assert total == 306 and len(rows) == 306
    fut = next(
        r for r in rows if r.instrument == "IDF" and r.symbol == "NIFTY" and r.expiry == date(2026, 10, 27)
    )
    assert (fut.right, fut.strike, fut.settle, fut.close) == (
        "XX",
        0,
        Decimal("22707.30"),
        Decimal("22707.30"),
    )
    assert (fut.oi, fut.chg_oi, fut.contracts) == (18015075, 879840, 58953)
    assert (fut.underlying, fut.lot_size, fut.turnover) == (
        Decimal("22620.45"),
        65,
        Decimal("87398947629.50"),
    )
    ce = next(r for r in rows if r.symbol == "NIFTY" and r.expiry == date(2026, 10, 27) and r.strike == 22700
              and r.right == "CE")  # fmt: skip
    assert (ce.open, ce.high, ce.low, ce.close, ce.settle) == tuple(
        Decimal(x) for x in ("362.70", "439.00", "308.85", "316.25", "316.25"))  # fmt: skip
    # OI is in units (a multiple of the lot), volume in contracts
    assert ce.oi % ce.lot_size == 0 and ce.contracts == 47709 and ce.day == date(2026, 9, 30)


def test_udiff_volume_is_contracts_turnover_is_units_times_price():
    """Turnover = contracts x lot x (strike + premium) for options: checks the unit convention independently."""
    text = UDIFF.splitlines()[0] + "\n" + ("2026-09-30,2026-09-30,FO,NSE,IDO,67782,,NIFTY,,2028-12-26,2028-12-26,24000.00,"
                                           "CE,NIFTY28DEC24000CE,3064.95,3065.00,3064.95,3065.00,3065.00,3015.00,22620.45,"
                                           "3078.16,2665,-65,2,3518446.75,2,F1,65,,,,,\n")  # fmt: skip
    (r,), _ = fb.parse_udiff(text)
    assert r.contracts * r.lot_size * (r.strike + Decimal("3065.00")) == Decimal("3518450.00")  # ~ TtlTrfVal
    assert r.oi == 41 * 65


def test_legacy_fields_units_and_unknowns():
    rows, total = fb.parse_legacy(LEGACY)
    assert total == 117 and len(rows) == 117
    fut = next(r for r in rows if r.instrument == "IDF" and r.expiry == date(2024, 7, 25))
    assert (fut.symbol, fut.right, fut.strike, fut.settle) == ("NIFTY", "XX", 0, Decimal("24379.4"))
    assert (fut.contracts, fut.oi, fut.chg_oi) == (230381, 14171325, -371200)
    assert fut.turnover == Decimal("1400814.91") * 100000  # lakh -> rupees
    assert fut.underlying is None and fut.lot_size is None  # not in the legacy file: unknown, never 0
    stale = next(r for r in rows if r.strike == 21100 and r.right == "CE")
    assert (stale.close, stale.settle, stale.contracts) == (Decimal("1897.1"), Decimal("3248.5"), 0)
    assert {r.instrument for r in rows} == {"IDF", "IDO", "STF", "STO"} and fut.day == date(2024, 7, 5)


def test_keep_filter_and_wrong_header():
    rows, total = fb.parse_udiff(UDIFF, keep=lambda kind, sym: sym == "RELIANCE")
    assert total == 306 and {r.symbol for r in rows} == {"RELIANCE"} and len(rows) == 30
    with pytest.raises(NseError):
        fb.parse_udiff(LEGACY)
    with pytest.raises(NseError):
        fb.parse_legacy(UDIFF)


def test_routes_by_date():
    assert fb.formats_for(date(2023, 12, 29)) == ["legacy"]
    assert fb.formats_for(date(2024, 1, 1)) == ["udiff", "legacy"]
    assert fb.formats_for(date(2024, 7, 8)) == ["udiff"]
    assert fb.url_for(date(2022, 1, 25), "legacy") == (
        "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/2022/JAN/fo25JAN2022bhav.csv.zip"
    )
    assert fb.url_for(date(2026, 9, 30), "udiff") == (
        "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20260930_F_0000.csv.zip"
    )


def test_unzip_refuses_non_zip_and_parses_zip():
    with pytest.raises(NseError):
        fb.unzip_csv(b"<!DOCTYPE html>")
    rows, _ = fb.parse(zipped("BhavCopy.csv", UDIFF), "udiff")
    assert len(rows) == 306


async def _no_sleep(_s: float) -> None:
    return None


def _client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


@respx.mock
async def test_fetch_tristate_404_zip_and_block_page():
    day = date(2024, 3, 1)
    respx.get(fb.url_for(day, "udiff")).mock(return_value=httpx.Response(404, text="<html>not found</html>"))
    respx.get(fb.url_for(day, "legacy")).mock(
        return_value=httpx.Response(200, content=zipped("fo.csv", LEGACY))
    )
    async with _client() as http:
        got = await fb.fetch_day(http, day)  # UDiFF 404 before 08-Jul-2024: the legacy route answers
        assert got is not None and got[0] == "legacy"
        holiday = date(2026, 10, 2)
        respx.get(fb.url_for(holiday, "udiff")).mock(return_value=httpx.Response(404, text="x" * 3425))
        assert await fb.fetch_day(http, holiday) is None
        blocked = date(2026, 10, 5)
        respx.get(fb.url_for(blocked, "udiff")).mock(
            return_value=httpx.Response(200, text="<html>Access Denied")
        )
        with pytest.raises(NseError):  # a 200 block page is never "no file"
            await fb.fetch_day(http, blocked)
        refused = date(2026, 10, 6)
        respx.get(fb.url_for(refused, "udiff")).mock(return_value=httpx.Response(403))
        with pytest.raises(NseError):
            await fb.fetch_day(http, refused)

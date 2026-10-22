"""#366: FBIL's pre-2023 G-sec workbooks are OLE2 .xls; the reader decision (xlrd) and the format dispatch."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters import fbil as fbil_mod
from finresearch.adapters import xls
from finresearch.adapters.fbil import FbilClient, FbilError, parse_par_yield
from finresearch.adapters.http import PoliteClient

FIX = Path(__file__).parent / "fixtures" / "fbil"


def test_read_sheets_reads_an_ole2_workbook_as_rows():
    sheets = xls.read_sheets((FIX / "gsec_synthetic_2022.xls").read_bytes())
    assert list(sheets) == ["G-Sec", "Par-Yield", "SDL", "Special"]
    par = sheets["Par-Yield"]
    assert par[2][:2] == ["FBIL GSec Base/Par Yield", "03 Jan, 2022"]
    assert par[5] == [0.25, 4.0, pytest.approx(4.04)]


def test_read_sheets_reads_xlsx_too():
    sheets = xls.read_sheets((FIX / "gsec_20260923_trimmed.xlsx").read_bytes())
    assert any("par" in n.lower() for n in sheets)


def test_read_sheets_refuses_a_non_workbook():
    with pytest.raises(xls.NotAWorkbook):
        xls.read_sheets(b"<html>blocked</html>")


def test_par_curve_from_the_ole2_workbook():
    curve = parse_par_yield((FIX / "gsec_synthetic_2022.xls").read_bytes())
    assert curve.as_of == date(2022, 1, 3)  # '03 Jan, 2022': the .xls era's date format
    assert len(curve.points) == 8
    first, last = curve.points[0], curve.points[-1]
    assert (first.tenor_years, first.semi_annual) == (Decimal("0.25"), Decimal("0.04"))
    assert last.tenor_years == Decimal("2.0") and last.semi_annual == Decimal("0.047")


def test_par_curve_from_the_xlsx_workbook_unchanged():
    curve = parse_par_yield((FIX / "gsec_20260923_trimmed.xlsx").read_bytes())
    assert curve.as_of == date(2026, 9, 23)


def test_par_curve_refuses_a_block_page():
    with pytest.raises(FbilError):
        parse_par_yield(b"<html>blocked</html>")


@respx.mock
async def test_client_accepts_an_ole2_workbook():
    respx.get(fbil_mod.DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, content=(FIX / "gsec_synthetic_2022.xls").read_bytes())
    )
    async with FbilClient(PoliteClient(cache_dir=None, clock=lambda: 0.0)) as c:
        curve = await c.par_curve(date(2022, 1, 3))
    assert curve.as_of == date(2022, 1, 3) and "date=2022-01-03" in curve.source

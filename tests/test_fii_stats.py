"""FII derivative statistics (#371): NSE's daily fii_stats .xls, the resumable backfill, the nightly slot, lineage and
the data-quality checks. Synthetic workbooks only, written in each test with xlwt (made-up numbers in the layout the
adapter expects); no network: respx answers every request."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx
import xlwt

from finresearch.adapters import nse_fii_stats as fs
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NseError

D = date(2020, 10, 9)  # a Friday
# made-up (buy contracts, buy amt, sell contracts, sell amt, OI contracts, OI amt) per category
ROWS = {
    "INDEX FUTURES": (41_234, 3_210.55, 39_876, 3_101.2, 160_500, 12_480.75),
    "INDEX OPTIONS": (1_900_321, 150_210.1, 1_880_002, 148_900.95, 950_000, 75_300.0),
    "STOCK FUTURES": (90_120, 6_010.4, 88_765, 5_920.35, 1_120_400, 74_890.6),
    "STOCK OPTIONS": (120_045, 8_100.25, 119_800, 8_090.0, 140_200, 9_450.3),
}


def synth_xls(day: date = D, rows: dict[str, tuple] = ROWS, *, title: str | None = None,
              header: tuple[str, ...] = ("BUY", "SELL", "OPEN INTEREST AT THE END OF THE DAY"),
              notes: bool = True) -> bytes:  # fmt: skip
    """An OLE2 .xls in the expected layout: a title row, a BUY/SELL/OI header over two columns each, a sub-header
    (No. of contracts / Amt in Crores), one row per category, then free-text notes."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    ws.write(0, 0, title if title is not None else f"FII DERIVATIVES STATISTICS FOR {day:%d-%b-%Y}")
    for i, h in enumerate(header):
        ws.write(2, 1 + 2 * i, h)
    for i in range(3):
        ws.write(3, 1 + 2 * i, "No. of contracts")
        ws.write(3, 2 + 2 * i, "Amt in Crores")
    for r, (label, vals) in enumerate(rows.items(), start=4):
        ws.write(r, 0, label)
        for c, v in enumerate(vals, start=1):
            if v is not None:
                ws.write(r, c, v)
    if notes:
        ws.write(5 + len(rows), 0, "Note: synthetic workbook for tests; the numbers are made up")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


# ------------------------------------------------------------------------------------------------------- adapter
def test_url_names_the_day_as_dd_mon_yyyy():
    assert fs.url_for(D) == "https://nsearchives.nseindia.com/content/fo/fii_stats_09-Oct-2020.xls"


def test_parse_reads_every_category_with_whole_contracts_and_decimal_amounts():
    got = fs.parse(synth_xls())
    assert got.day == D and list(got.rows) == list(ROWS)
    fut = got.rows["INDEX FUTURES"]
    assert fut == {"buy_contracts": 41_234, "buy_amt_cr": Decimal("3210.55"), "sell_contracts": 39_876,
                   "sell_amt_cr": Decimal("3101.2"), "oi_contracts": 160_500, "oi_amt_cr": Decimal("12480.75")}  # fmt: skip
    assert isinstance(fut["buy_contracts"], int)


def test_extra_categories_are_kept_and_labels_normalised():
    rows = {**ROWS, "  nifty   futures ": (1, 0.5, 2, 1.0, 3, 1.5)}
    assert fs.parse(synth_xls(rows=rows)).rows["NIFTY FUTURES"]["oi_contracts"] == 3


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"title": "SOMETHING ELSE"}, "title"),
        ({"header": ("BUY", "SELL", "TURNOVER")}, "header"),
        ({"rows": {k: v for k, v in ROWS.items() if k != "STOCK OPTIONS"}}, "STOCK OPTIONS"),
        ({"rows": {**ROWS, "INDEX FUTURES": (1.5, 1, 1, 1, 1, 1)}}, "whole"),
        ({"rows": {**ROWS, "INDEX FUTURES": (-1, 1, 1, 1, 1, 1)}}, "negative"),
        ({"rows": {**ROWS, "INDEX FUTURES": (1, -0.5, 1, 1, 1, 1)}}, "negative"),
        ({"rows": {**ROWS, "INDEX FUTURES": (1, 1, None, 1, 1, 1)}}, "INDEX FUTURES"),
        ({"rows": {**ROWS, "INDEX FUTURES": (1, 1, "n/a", 1, 1, 1)}}, "INDEX FUTURES"),
    ],
)
def test_parse_refuses_a_layout_it_does_not_know(change, match):
    with pytest.raises(fs.FiiStatsError, match=match):
        fs.parse(synth_xls(**change))


def test_parse_refuses_a_non_workbook():
    with pytest.raises(fs.FiiStatsError, match="workbook"):
        fs.parse(b"<html>Access Denied</html>")


@respx.mock
async def test_fetch_day_returns_the_body_as_served_and_none_on_404():
    body = synth_xls()
    respx.get(fs.url_for(D)).mock(return_value=httpx.Response(200, content=body))
    respx.get(fs.url_for(date(2020, 10, 12))).mock(return_value=httpx.Response(404))
    async with client() as http:
        assert await fs.fetch_day(http, D) == (fs.url_for(D), body)
        assert await fs.fetch_day(http, date(2020, 10, 12)) is None


@respx.mock
@pytest.mark.parametrize("resp", [httpx.Response(200, text="<html>Access Denied</html>"), httpx.Response(403)])
async def test_fetch_day_raises_on_a_block_page_or_refusal(resp):
    respx.get(fs.url_for(D)).mock(return_value=resp)
    async with client() as http:
        with pytest.raises(NseError):
            await fs.fetch_day(http, D)

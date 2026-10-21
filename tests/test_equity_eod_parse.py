"""NSE's daily full bhavcopy with delivery, sec_bhavdata_full_DDMMYYYY.csv (#373 (a)): space-padded cells, '-' as
unknown (never 0), every series kept, and a block page or a wrong-day file refused. The fixture is synthetic (made-up
symbols and values in NSE's column layout, checked against the 21-Oct-2026 file's header on 22-Oct-2026)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters import nse_equity_eod as ee
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NseError

FX = Path(__file__).parent / "fixtures" / "equity_eod"
FILE = (FX / "sec_bhavdata_full_synthetic.csv").read_text()
D = date(2024, 1, 15)


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


def test_url_uses_ddmmyyyy():
    assert ee.url_for(D) == "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_15012024.csv"


def test_reads_every_column_of_an_eq_row():
    rows = {(r.symbol, r.series): r for r in ee.parse(FILE, D)}
    a = rows[("ALPHACO", "EQ")]
    assert a.day == D
    assert (a.prev_close, a.open, a.high, a.low, a.last, a.close, a.avg_price) == (
        Decimal("100.00"), Decimal("101.00"), Decimal("105.50"), Decimal("99.25"), Decimal("104.10"),
        Decimal("104.00"), Decimal("102.37"))  # fmt: skip
    assert (a.qty, a.turnover_lacs, a.trades, a.deliv_qty, a.deliv_pct) == (
        200000, Decimal("204.74"), 5000, 120000, Decimal("60.00"))  # fmt: skip


def test_every_series_is_kept_and_a_symbol_may_have_several():
    keys = [(r.symbol, r.series) for r in ee.parse(FILE, D)]
    assert keys == [
        ("ALPHACO", "EQ"),
        ("BETAIND", "EQ"),
        ("GAMMALTD", "BE"),
        ("DELTASM", "SM"),
        ("ALPHACO", "N1"),
    ]


def test_dash_delivery_is_unknown_never_zero():
    g = next(r for r in ee.parse(FILE, D) if r.symbol == "GAMMALTD")
    assert g.deliv_qty is None and g.deliv_pct is None and g.qty == 216919


def test_block_page_and_wrong_day_are_refused():
    with pytest.raises(NseError):
        ee.parse("<html><body>Access Denied</body></html>", D)
    with pytest.raises(NseError, match="another day"):
        ee.parse(FILE, date(2024, 1, 16))


def test_a_row_with_an_unreadable_close_is_unknown_not_zero():
    bad = FILE.replace("2491.35", "-")
    b = next(r for r in ee.parse(bad, D) if r.symbol == "BETAIND")
    assert b.close is None


@respx.mock
async def test_fetch_day_404_is_no_file_and_html_200_is_an_error():
    respx.get(ee.url_for(D)).mock(return_value=httpx.Response(404, text="<html>not found</html>"))
    respx.get(ee.url_for(date(2024, 1, 16))).mock(
        return_value=httpx.Response(200, text="<html>blocked</html>")
    )
    respx.get(ee.url_for(date(2024, 1, 17))).mock(return_value=httpx.Response(200, text=FILE))
    async with client() as http:
        assert await ee.fetch_day(http, D) is None
        with pytest.raises(NseError, match="block page"):
            await ee.fetch_day(http, date(2024, 1, 16))
        url, body = await ee.fetch_day(http, date(2024, 1, 17))
    assert url == ee.url_for(date(2024, 1, 17)) and body.startswith(b"SYMBOL")


@respx.mock
async def test_fetch_day_refusal_raises_with_the_status():
    respx.get(ee.url_for(D)).mock(return_value=httpx.Response(403))
    async with client() as http:
        with pytest.raises(NseError, match="HTTP 403"):
            await ee.fetch_day(http, D)

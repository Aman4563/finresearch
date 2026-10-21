"""NSE's daily index close file ind_close_all_DDMMYYYY.csv (#366): both eras' names and date styles, '-' as unknown,
and a block page or a wrong-day file refused. Fixtures are synthetic (made-up values in NSE's column layout)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters import nse_index_eod as ie
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NseError

FX = Path(__file__).parent / "fixtures" / "index_eod"
LEGACY = (FX / "ind_close_all_legacy_synthetic.csv").read_text()
CURRENT = (FX / "ind_close_all_current_synthetic.csv").read_text()


def test_url_uses_ddmmyyyy():
    assert ie.url_for(date(2022, 1, 3)) == (
        "https://nsearchives.nseindia.com/content/indices/ind_close_all_03012022.csv"
    )


def test_current_file_maps_fno_symbols_and_reads_ratios():
    rows = {r.symbol: r for r in ie.parse(CURRENT, date(2022, 1, 3))}
    assert set(rows) >= {"NIFTY", "NIFTYNXT50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "INDIA VIX"}
    n = rows["NIFTY"]
    assert n.index_name == "Nifty 50" and n.day == date(2022, 1, 3)
    assert (n.open, n.high, n.low, n.close) == (Decimal("17400.00"), Decimal("17650.00"), Decimal("17380.00"),
                                                Decimal("17600.50"))  # fmt: skip
    assert (n.pe, n.pb, n.div_yield) == (Decimal("24.50"), Decimal("4.40"), Decimal("1.20"))
    assert n.volume == 200000000 and n.turnover_cr == Decimal("16000.00")
    assert rows["INDIA VIX"].close == Decimal("16.45")


def test_dash_is_unknown_never_zero():
    rows = {r.symbol: r for r in ie.parse(CURRENT, date(2022, 1, 3))}
    vix = rows["INDIA VIX"]
    assert vix.pe is None and vix.pb is None and vix.div_yield is None and vix.volume is None
    arb = rows["NIFTY 50 ARBITRAGE"]  # an unmapped index keeps its upper-cased name
    assert arb.open is None and arb.close == Decimal("2000.00") and arb.pct_change == Decimal("0.03")


def test_legacy_cnx_names_and_slash_dates():
    rows = {r.symbol: r for r in ie.parse(LEGACY, date(2015, 6, 1))}
    assert rows["NIFTY"].index_name == "CNX Nifty" and rows["NIFTY"].close == Decimal("8420.75")
    assert rows["NIFTYNXT50"].index_name == "CNX Nifty Junior"
    assert rows["BANKNIFTY"].close == Decimal("18650.00")
    assert rows["INDIA VIX"].close == Decimal("16.75")
    assert rows["CNX NIFTY DIVIDEND"].open is None


def test_a_file_for_another_day_is_refused():
    with pytest.raises(NseError, match="another day"):
        ie.parse(CURRENT, date(2022, 1, 4))


def test_a_block_page_is_not_a_file():
    with pytest.raises(NseError, match="not an NSE index close file"):
        ie.parse('<!DOCTYPE html>\n<html lang="en">', date(2008, 10, 10))


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


@respx.mock
async def test_fetch_404_is_no_file_but_403_and_html_raise():
    d404, d403, dhtml, dok = date(2013, 1, 1), date(2013, 1, 2), date(2013, 1, 3), date(2022, 1, 3)
    respx.get(ie.url_for(d404)).mock(return_value=httpx.Response(404, text="<html>not found</html>"))
    respx.get(ie.url_for(d403)).mock(return_value=httpx.Response(403))
    respx.get(ie.url_for(dhtml)).mock(return_value=httpx.Response(200, text="<!DOCTYPE html><html></html>"))
    respx.get(ie.url_for(dok)).mock(return_value=httpx.Response(200, text=CURRENT))
    async with client() as http:
        assert await ie.fetch_day(http, d404) is None
        with pytest.raises(NseError, match="HTTP 403"):
            await ie.fetch_day(http, d403)
        with pytest.raises(NseError, match="block page"):
            await ie.fetch_day(http, dhtml)
        url, body = await ie.fetch_day(http, dok)
    assert url == ie.url_for(dok) and body.startswith(b"Index Name")

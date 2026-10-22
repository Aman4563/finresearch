"""The Nifty 50's members (NSE's constituent CSV) with each one's free-float market cap from its NSE quote (#348,
adapters.nse_index_weights). Hand-made synthetic CSV (made-up symbols and ISINs), fake quotes, NSE mocked. Offline."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from finresearch.adapters import nse_index_weights as nw
from finresearch.adapters.http import PoliteClient

CSV = (Path(__file__).parent / "fixtures" / "nse_index" / "ind_nifty50list_synthetic.csv").read_text()
PRICES = Path(__file__).parent / "fixtures" / "prices"


def test_quote_parses_the_free_float_market_cap():
    from finresearch.adapters.nse import Quote

    q = Quote.parse(json.loads((PRICES / "nse_quote_INFY_20260930_after_close.json").read_text()))
    assert q.ffmc == Decimal("3501433699412.84") and q.sector == "Information Technology"


def test_parse_constituents():
    rows = nw.parse_constituents(CSV)
    assert [r.symbol for r in rows] == ["EXBANK", "EXSOFT", "EXNEW"]
    assert rows[0] == nw.Constituent("EXBANK", "Example Bank Ltd.", "Financial Services", "INE000X01011")
    with pytest.raises(nw.IndexWeightsError, match="not NSE's index constituent list"):
        nw.parse_constituents("<html>blocked</html>")
    with pytest.raises(nw.IndexWeightsError, match="no constituents"):
        nw.parse_constituents("Company Name,Industry,Symbol,Series,ISIN Code\n")


async def test_members_take_the_cap_from_each_quote_and_a_failed_quote_stays_unknown():
    async def quote(sym):
        if sym == "EXNEW":
            raise RuntimeError("NSE quote refused for EXNEW: HTTP 403")
        return SimpleNamespace(ffmc=Decimal({"EXBANK": "9000.5", "EXSOFT": "3000"}[sym]),
                               last_price=Decimal({"EXBANK": "1500", "EXSOFT": "1200"}[sym]))  # fmt: skip

    iw = await nw.weights(nw.parse_constituents(CSV), quote, url="https://example.invalid/list.csv")
    by = {m.symbol: m for m in iw.members}
    assert (by["EXBANK"].ffmc, by["EXBANK"].last_price, by["EXBANK"].sector) == (
        9000.5,
        1500.0,
        "Financial Services",
    )
    assert by["EXNEW"].ffmc is None and by["EXNEW"].reason == "its NSE quote failed: RuntimeError"
    assert iw.unreachable == ["EXNEW"] and iw.url == "https://example.invalid/list.csv"


async def _no_sleep(_s: float) -> None:
    return None


def _http() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


async def _quote(sym):
    return SimpleNamespace(ffmc=Decimal(100), last_price=Decimal(10))


@respx.mock
async def test_client_reads_the_list_through_the_polite_client():
    route = respx.get(nw.LIST_URL).mock(return_value=httpx.Response(200, text=CSV))
    http = _http()
    async with nw.NiftyWeightsClient(http) as c:
        iw = await c.nifty50(_quote)
    await http.aclose()
    assert route.called and len(iw.members) == 3 and iw.fetched_at is not None and not iw.unreachable


@respx.mock
async def test_client_refusal_is_an_error_not_an_empty_index():
    respx.get(nw.LIST_URL).mock(return_value=httpx.Response(503))
    http = _http()
    with pytest.raises(nw.IndexWeightsError, match="HTTP 503"):
        async with nw.NiftyWeightsClient(http) as c:
            await c.nifty50(_quote)
    await http.aclose()

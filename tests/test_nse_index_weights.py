"""NSE's Nifty 50 constituents with free-float market caps (#348, adapters.nse_index_weights): parsing a hand-made
synthetic payload (made-up symbols and ISINs) and the client over a mocked NSE. Offline."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters import nse_index_weights as nw
from finresearch.adapters.http import PoliteClient

FX = Path(__file__).parent / "fixtures" / "nse_index"
PAYLOAD = json.loads((FX / "nifty50_synthetic.json").read_text())


def test_parse_skips_the_index_row_and_keeps_a_missing_ffmc_unknown():
    iw = nw.parse(PAYLOAD, url="https://example.invalid/x")
    assert iw.index == "NIFTY 50" and iw.as_of == datetime(2026, 10, 20, 15, 30, tzinfo=iw.as_of.tzinfo)
    by = {m.symbol: m for m in iw.members}
    assert set(by) == {"EXBANK", "EXSOFT", "EXNEW"}  # the "NIFTY 50" row itself is not a member
    assert by["EXBANK"].ffmc == pytest.approx(9000000000000.5) and by["EXBANK"].last_price == 1500.0
    assert by["EXBANK"].name == "Example Bank Limited" and by["EXBANK"].isin == "INE000X01011"
    assert by["EXNEW"].ffmc is None  # absent: unknown, never 0


def test_parse_refuses_a_payload_without_members():
    with pytest.raises(nw.IndexWeightsError, match="no constituents"):
        nw.parse({"name": "NIFTY 50", "data": [PAYLOAD["data"][0]]}, url="u")
    with pytest.raises(nw.IndexWeightsError, match="not the NIFTY 50"):
        nw.parse({**PAYLOAD, "name": "NIFTY BANK"}, url="u")


async def _no_sleep(_s: float) -> None:
    return None


@respx.mock
async def test_client_reads_through_the_polite_client():
    respx.get(nw.WARMUP_URL).mock(return_value=httpx.Response(200, text="<html>ok</html>"))
    route = respx.get(url__startswith=nw.API_URL).mock(
        return_value=httpx.Response(200, json=PAYLOAD, headers={"content-type": "application/json"})
    )
    http = PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)
    async with nw.NiftyWeightsClient(http) as c:
        iw = await c.nifty50()
    await http.aclose()
    assert route.called and route.calls.last.request.url.params["index"] == "NIFTY 50"
    assert len(iw.members) == 3 and iw.url.startswith(nw.API_URL) and iw.fetched_at is not None


def test_member_sector_is_nse_classification_or_unknown():
    by = {m.symbol: m for m in nw.parse(PAYLOAD, url="u").members}
    assert (
        by["EXBANK"].sector == "Private Sector Bank"
        and by["EXSOFT"].sector == "Computers - Software & Consulting"
    )
    row = {**PAYLOAD["data"][1], "meta": {"companyName": "X"}}
    assert nw.parse({**PAYLOAD, "data": [row]}, url="u").members[0].sector is None

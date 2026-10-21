"""NSE's SPAN risk-parameter file (#370): the golden scan ranges, the risk-array sign convention, the zip checks and
FOVOLT. Fixtures are synthetic (NSE's layout; NIFTY's price and priceScan are the figures quoted in the issue)."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from finresearch.adapters import nse_span as sp
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NseError

FX = Path(__file__).parent / "fixtures" / "span"
SPN = (FX / "nsccl_synthetic.spn").read_bytes()
VOLT = (FX / "FOVOLT_synthetic.csv").read_text()
DAY = date(2026, 10, 16)


def zipped(body: bytes, name: str = "nsccl.20261016.s.spn") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, body)
    return buf.getvalue()


def test_urls():
    assert sp.url_for(DAY) == "https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.20261016.s.zip"
    assert sp.volt_url_for(DAY) == "https://nsearchives.nseindia.com/archives/nsccl/volt/FOVOLT_16102026.csv"


def test_golden_scan_ranges():
    f = sp.parse(zipped(SPN))
    assert f.day == DAY and f.created == "202610161654"
    n, b = f.params["NIFTY"], f.params["BANKNIFTY"]
    assert (
        n.price == Decimal("23146.150000")
        and n.price_scan == Decimal("2152.59")
        and n.vol_scan == Decimal("0.04")
    )
    # 2152.59 / 23146.15 = 0.092999...; 4650 / 50000 = 0.093
    assert round(n.scan_pct, 4) == Decimal("0.0930") and round(b.scan_pct, 4) == Decimal("0.0930")
    assert f.params["FAKESTK"].scan_pct == Decimal("0.15")


def test_som_rate_and_spread_tiers():
    f = sp.parse(zipped(SPN))
    assert f.params["NIFTY"].som_rate == Decimal("0") and f.params["BANKNIFTY"].som_rate == Decimal("12.5")
    assert f.params["FAKESTK"].som_rate is None  # no ccDef in the file: unknown, never 0
    assert f.params["NIFTY"].spreads == [{"spread": 1, "charge": "F", "rate": "400.000000",
                                          "legs": [["20261019", "A", "1.000000"], ["20261027", "B", "1.000000"]]}]  # fmt: skip


def test_scenario_definitions_are_read_from_the_file():
    f = sp.parse(zipped(SPN))
    assert len(f.points) == 16
    assert f.points[14] == {"point": 15, "price_mult": 3.0, "vol_mult": 0.0, "weight": 0.3}


def test_risk_array_sign_convention_long_put_loses_its_premium_when_price_rises():
    arrays = sp.risk_arrays(zipped(SPN), "NIFTY")
    pe = next(a for a in arrays if a.kind == "OPT" and a.right == "P" and a.strike == Decimal("23200.00"))
    assert pe.price == Decimal("146.95") and pe.expiry == date(2026, 10, 19)
    # points 3/4, 7/8, 11/12 move the price up: a long put's per-unit loss is its whole premium
    for i in (2, 3, 6, 7, 10, 11):
        assert pe.values[i] == Decimal("146.95"), i
    fut = next(a for a in arrays if a.kind == "FUT")
    assert fut.values[10] < 0  # price up: a long future gains, so its loss is negative


def test_not_a_span_zip_is_refused():
    with pytest.raises(NseError, match="zip"):
        sp.parse(b"<!DOCTYPE html><html></html>")
    with pytest.raises(NseError, match="unexpected"):
        sp.parse(zipped(SPN, "other.csv"))
    with pytest.raises(NseError, match="SPAN"):
        sp.parse(zipped(b"<?xml version='1.0'?><html/>"))


def test_fovolt():
    rows = {r.symbol: r for r in sp.parse_volt(VOLT, DAY)}
    n = rows["NIFTY"]
    assert n.underlying_close == Decimal("23146.15") and n.applicable_annual_vol == Decimal("0.16221164")
    # the EWMA NSE prints: sqrt(0.995 x D^2 + 0.005 x C^2)
    assert float(n.underlying_daily_vol) == pytest.approx(
        (0.995 * float(n.underlying_prev_vol) ** 2 + 0.005 * float(n.underlying_log_return) ** 2) ** 0.5,
        abs=1e-8,
    )
    with pytest.raises(NseError, match="another day"):
        sp.parse_volt(VOLT, date(2026, 10, 15))


async def _no_sleep(_s: float) -> None:
    return None


@respx.mock
async def test_fetch_semantics():
    respx.get(sp.url_for(DAY)).mock(return_value=httpx.Response(200, content=zipped(SPN)))
    respx.get(sp.url_for(date(2026, 10, 17))).mock(return_value=httpx.Response(404, text="<html/>"))
    respx.get(sp.url_for(date(2026, 10, 19))).mock(
        return_value=httpx.Response(200, text="<html>blocked</html>")
    )
    respx.get(sp.url_for(date(2026, 10, 20))).mock(return_value=httpx.Response(429))
    async with PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0) as http:
        url, body = await sp.fetch_day(http, DAY)
        assert url == sp.url_for(DAY) and body.startswith(b"PK")
        assert await sp.fetch_day(http, date(2026, 10, 17)) is None
        with pytest.raises(NseError, match="block page"):
            await sp.fetch_day(http, date(2026, 10, 19))
        with pytest.raises(NseError, match="HTTP 429"):
            await sp.fetch_day(http, date(2026, 10, 20))

"""IIMA Indian Fama-French-momentum factor library adapter (#348): offline, on synthetic fixtures only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from finresearch.adapters import iima_factors as iima
from finresearch.adapters.http import PoliteClient

FIX = Path(__file__).parent / "fixtures" / "iima"
CSV = (FIX / "factors_daily_synthetic.csv").read_bytes()
INDEX = (FIX / "index_synthetic.html").read_text()


async def _no_sleep(_: float) -> None:
    return None


def test_parse_percent_to_fraction_and_skip_na_rows():
    s = iima.parse_daily(CSV.decode(), release="2024-12", url="u")
    assert s.first == date(2024, 1, 2) and s.last == date(2024, 1, 5)
    assert len(s.rows) == 4 and s.skipped == [date(2024, 1, 1)]
    r = s.rows[date(2024, 1, 2)]
    # percent holding-period returns → fractions; MF is already the market premium Rm - Rf
    assert r.mf == pytest.approx(0.0125) and r.smb == pytest.approx(0.0012)
    assert r.hml == pytest.approx(-0.003) and r.wml == pytest.approx(0.004) and r.rf == pytest.approx(0.00025)


def test_parse_refuses_a_page_that_is_not_the_factor_file():
    with pytest.raises(iima.IimaError, match="not the IIMA daily factor file"):
        iima.parse_daily("<html>Moved</html>", release="x", url="u")
    with pytest.raises(iima.IimaError, match="no rows"):
        iima.parse_daily("Date,SMB,HML,WML,MF,RF\r\n", release="x", url="u")


def test_latest_daily_link_picks_the_newest_survivorship_adjusted_daily_file():
    url, release = iima.latest_daily_link(INDEX)
    assert release == "2024-12"
    assert url == f"{iima.BASE}DATA/2024-12_FourFactors_and_Market_Returns_Daily_SurvivorshipBiasAdjusted.csv"
    with pytest.raises(iima.IimaError, match="no daily factor file"):
        iima.latest_daily_link("<html>nothing here</html>")


async def test_client_fetches_index_then_file_and_caches_only_the_real_file(tmp_path):
    calls: list[str] = []
    csv_body = {"v": b"<html>maintenance</html>"}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith(".csv"):
            return httpx.Response(200, content=csv_body["v"])
        return httpx.Response(200, text=INDEX)

    def client() -> PoliteClient:
        return PoliteClient(cache_dir=tmp_path, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0,
                            transport=httpx.MockTransport(handler), slots=None)  # fmt: skip

    # a 200 maintenance page is an error, and is not cached as the factor file
    async with iima.IimaFactorsClient(client()) as c:
        with pytest.raises(iima.IimaError):
            await c.latest_daily()
    csv_body["v"] = CSV
    async with iima.IimaFactorsClient(client()) as c:
        s = await c.latest_daily()
    assert s.release == "2024-12" and len(s.rows) == 4
    n = len(calls)
    async with iima.IimaFactorsClient(client()) as c:
        again = await c.latest_daily()
    assert len(calls) == n  # index and file both served from the disk cache
    assert again.rows == s.rows and again.fetched_at == s.fetched_at

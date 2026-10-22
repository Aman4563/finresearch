"""NSE Clearing's combined OI (MWPL) file and the dated ban notice (P1.12, #374): parsing synthetic fixtures (made-up
symbols and ISINs) and the fetchers against respx. No network."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from optpanel_synth import zipped

from finresearch.adapters import nse_mwpl as mw
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NseError

FX = Path(__file__).parent / "fixtures" / "nse" / "mwpl"
OI_CSV = (FX / "combineoi_02032026_synthetic.csv").read_text()
BAN_CSV = (FX / "fo_secban_03032026_synthetic.csv").read_text()
DAY = date(2026, 3, 2)


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


def test_parse_combineoi_reads_mwpl_oi_and_utilisation() -> None:
    got = mw.parse_combineoi(OI_CSV, DAY)
    by = {r.symbol: r for r in got.rows}
    assert set(by) == {"ZZALPHA", "ZZBETA", "ZZGAMMA"} and got.oi_basis == "Open Interest"
    a = by["ZZALPHA"]
    assert (a.mwpl, a.oi, a.limit_next, a.isin) == (1_000_000, 960_000, 0, "INE000Z01011")
    assert a.utilisation == Decimal("0.96") and by["ZZBETA"].utilisation == Decimal("0.5")


def test_parse_prefers_the_future_equivalent_oi_column_when_present() -> None:
    text = OI_CSV.replace("Open Interest,", "Open Interest,Future Equivalent Open Interest,", 1)
    lines = text.splitlines()
    lines[1:] = [x.replace(",960000,", ",960000,940000,").replace(",1000000,9", ",1000000,1000000,9")
                 .replace(",450000,", ",450000,440000,") for x in lines[1:]]  # fmt: skip
    got = mw.parse_combineoi("\n".join(lines) + "\n", DAY)
    assert got.oi_basis == "Future Equivalent Open Interest"
    assert {r.symbol: r.oi for r in got.rows}["ZZALPHA"] == 940_000


@pytest.mark.parametrize(
    "body",
    ["<html><body>Access Denied</body></html>", "Date,ISIN,Scrip Name\n02-Mar-2026,X,Y\n", ""],
)
def test_parse_refuses_anything_but_the_combined_oi_file(body: str) -> None:
    with pytest.raises(NseError):
        mw.parse_combineoi(body, DAY)


def test_parse_refuses_a_file_dated_another_day() -> None:
    with pytest.raises(NseError, match="another day"):
        mw.parse_combineoi(OI_CSV, date(2026, 3, 3))


def test_a_zero_or_missing_mwpl_leaves_utilisation_unknown() -> None:
    text = OI_CSV.replace("2000000,1000000", "0,1000000").replace("500000,450000", "-,450000")
    by = {r.symbol: r for r in mw.parse_combineoi(text, DAY).rows}
    assert (
        by["ZZBETA"].utilisation is None and by["ZZGAMMA"].utilisation is None and by["ZZGAMMA"].mwpl is None
    )


async def test_fetch_combineoi_unzips_and_maps_404_to_none() -> None:
    url = mw.combineoi_url(DAY)
    async with respx.mock(assert_all_called=True) as m:
        m.get(url).mock(return_value=httpx.Response(200, content=zipped(OI_CSV, "combineoi_02032026.csv")))
        m.get(mw.combineoi_url(date(2026, 3, 3))).mock(return_value=httpx.Response(404, text="nf"))
        http = client()
        got = await mw.fetch_combineoi(http, DAY)
        assert got is not None and got[0] == url and mw.unzip(got[1]) == OI_CSV
        assert await mw.fetch_combineoi(http, date(2026, 3, 3)) is None
        await http.aclose()


@pytest.mark.parametrize(
    ("status", "body", "msg"), [(403, b"denied", "HTTP 403"), (200, b"<html>blocked</html>", "block page")]
)
async def test_fetch_combineoi_raises_on_a_refusal_or_block_page(status: int, body: bytes, msg: str) -> None:
    async with respx.mock() as m:
        m.get(mw.combineoi_url(DAY)).mock(return_value=httpx.Response(status, content=body))
        http = client()
        with pytest.raises(NseError, match=msg):
            await mw.fetch_combineoi(http, DAY)
        await http.aclose()


async def test_fetch_ban_notice_parses_the_dated_archive() -> None:
    d = date(2026, 3, 3)
    async with respx.mock() as m:
        m.get(mw.secban_url(d)).mock(return_value=httpx.Response(200, text=BAN_CSV))
        m.get(mw.secban_url(date(2026, 3, 4))).mock(
            return_value=httpx.Response(200, text="<html>shell</html>")
        )
        http = client()
        url, body, ban = await mw.fetch_ban_notice(http, d)
        assert url.endswith("/archives/fo/sec_ban/fo_secban_03032026.csv") and ban.symbols == ["ZZALPHA"]
        assert body == BAN_CSV.encode()
        with pytest.raises(NseError):
            await mw.fetch_ban_notice(http, date(2026, 3, 4))  # a block page is never "nothing banned"
        await http.aclose()


async def test_fetch_ban_notice_refuses_a_notice_for_another_trade_day() -> None:
    d = date(2026, 3, 4)
    async with respx.mock() as m:
        m.get(mw.secban_url(d)).mock(return_value=httpx.Response(200, text=BAN_CSV))
        http = client()
        with pytest.raises(NseError, match="trade date"):
            await mw.fetch_ban_notice(http, d)
        await http.aclose()

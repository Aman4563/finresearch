"""Storing NSE's SPAN parameters (#370): idempotent loads, monthly sampling, resume, the 30-session raw window, the
lookup margin_approx uses, and the polite stops. Test database only; NSE mocked with respx; synthetic fixtures."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from finresearch.adapters import nse_span as sp
from finresearch.adapters.http import PoliteClient

FX = Path(__file__).parent / "fixtures" / "span"
SPN = (FX / "nsccl_synthetic.spn").read_text()
VOLT = (FX / "FOVOLT_synthetic.csv").read_text()
DAY = date(2026, 10, 16)
EVENING = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 19:30 IST


def spn_for(day: date) -> bytes:
    body = SPN.replace("<date>20261016</date>", f"<date>{day:%Y%m%d}</date>").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"nsccl.{day:%Y%m%d}.s.spn", body)
    return buf.getvalue()


def volt_for(day: date) -> str:
    return VOLT.replace("16-Oct-26", day.strftime("%d-%b-%y"))


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE span_param, span_day, fo_volt, fno_bhav_day"))

    wipe()
    yield env
    wipe()


def test_load_day_is_idempotent_and_keeps_only_parameters(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import SpanDay, SpanParam
    from finresearch.fnohist import span

    for _ in range(2):
        with session_scope() as s:
            assert span.load_day(s, DAY, sp.url_for(DAY), spn_for(DAY)) == 3
    with session_scope() as s:
        n = s.get(SpanParam, (DAY, "NIFTY"))
        assert (
            float(n.price_scan) == 2152.59
            and float(n.vol_scan) == 0.04
            and n.spreads[0]["rate"] == "400.000000"
        )
        m = s.get(SpanDay, DAY)
        assert m.status == "loaded" and m.rows == 3 and m.attempts == 2 and len(m.points) == 16


def test_params_for_reads_the_days_range_with_its_source(clean):
    from finresearch.db import session_scope
    from finresearch.fnohist import span

    with session_scope() as s:
        span.load_day(s, DAY, sp.url_for(DAY), spn_for(DAY))
    with session_scope() as s:
        got = span.params_for(s, "nifty", date(2026, 10, 19))
        assert got["params"]["price_scan"] == pytest.approx(2152.59 / 23146.15)
        assert got["params"]["vol_scan"] == pytest.approx(0.04)
        assert got["sources"]["price_scan"] == "NSE SPAN file 2026-10-16"
        assert span.params_for(s, "NIFTY", date(2026, 10, 30)) is None  # too old to be today's ranges
        assert (
            span.params_for(s, "NIFTY", date(2026, 10, 15)) is None
        )  # never a later file for an earlier day
        assert span.params_for(s, "UNKNOWN", DAY) is None


@respx.mock
async def test_monthly_backfill_takes_the_first_session_from_the_15th_and_resumes(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FoVolt, SpanDay
    from finresearch.fnohist import span

    # Aug 2026: 15th is a Saturday (skipped without a request), Mon 17th answers. Sep 2026: Tue 15th answers.
    aug, sep = date(2026, 8, 17), date(2026, 9, 15)
    for d in (aug, sep):
        respx.get(sp.url_for(d)).mock(return_value=httpx.Response(200, content=spn_for(d)))
        respx.get(sp.volt_url_for(d)).mock(return_value=httpx.Response(200, text=volt_for(d)))
    async with client() as http:
        res = await span.backfill(date(2026, 8, 1), date(2026, 9, 30), today=date(2026, 10, 21), http=http,
                                  holidays=set(), now=lambda: EVENING)  # fmt: skip
        assert res["loaded"] == 2 and res["no_file"] == 0 and not res["failed"] and res["volt_rows"] == 4
        assert respx.calls.call_count == 4  # two SPAN files and two FOVOLT files, nothing else
        again = await span.backfill(date(2026, 8, 1), date(2026, 9, 30), today=date(2026, 10, 21), http=http,
                                    holidays=set(), now=lambda: EVENING)  # fmt: skip
    assert again["planned"] == 0
    with session_scope() as s:
        assert s.get(SpanDay, aug).status == "loaded" and s.get(FoVolt, (sep, "NIFTY")) is not None
    assert not span.raw_dir().exists()  # backfilled files are parsed and discarded


@respx.mock
async def test_a_404_moves_to_the_next_day_of_the_month(clean):
    from finresearch.fnohist import span

    d15, d16 = date(2026, 9, 15), date(2026, 9, 16)
    respx.get(sp.url_for(d15)).mock(return_value=httpx.Response(404, text="<html/>"))
    respx.get(sp.url_for(d16)).mock(return_value=httpx.Response(200, content=spn_for(d16)))
    async with client() as http:
        res = await span.backfill(date(2026, 9, 1), date(2026, 9, 30), today=date(2026, 10, 21), http=http,
                                  holidays=set(), volt=False, now=lambda: EVENING)  # fmt: skip
    assert res["no_file"] == 1 and res["loaded"] == 1 and respx.calls.call_count == 2


@respx.mock
async def test_refusals_and_market_hours_stop_the_run(clean):
    from finresearch.fnohist import span

    respx.get(url__startswith=sp.ARCHIVES).mock(return_value=httpx.Response(403))
    async with client() as http:
        res = await span.backfill(date(2026, 1, 1), date(2026, 9, 30), today=date(2026, 10, 21), http=http,
                                  holidays=set(), now=lambda: EVENING)  # fmt: skip
        assert len(res["failed"]) == 2 and "refused" in res["stopped"] and respx.calls.call_count == 2
        market = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)  # 10:30 IST, a Wednesday
        res = await span.backfill(date(2026, 1, 1), date(2026, 9, 30), today=date(2026, 10, 21), http=http,
                                  holidays=set(), now=lambda: market)  # fmt: skip
    assert "market hours" in res["stopped"] and respx.calls.call_count == 2


def test_raw_zips_are_kept_for_the_last_30_sessions_only(clean):
    from datetime import timedelta

    from finresearch.fnohist import span

    for i in range(33):
        p = span.raw_path(date(2026, 8, 1) + timedelta(days=i))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PK")
    assert span.prune_raw() == 3
    left = sorted(p.name for p in span.raw_dir().glob("*/*.zip"))
    assert len(left) == 30 and left[0] == "nsccl.20260804.s.zip"


def test_risk_arrays_come_from_a_kept_zip(clean):
    from finresearch.fnohist import span

    assert span.arrays(DAY, "NIFTY") is None
    p = span.raw_path(DAY)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(spn_for(DAY))
    assert len(span.arrays(DAY, "NIFTY")) == 3


def test_span_has_a_recorded_data_rights_decision():
    from finresearch import data_rights

    assert data_rights.require("nse_span").choice == "accept-personal"

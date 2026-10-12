"""The nightly F&O bhavcopy job (#306): its window, holiday skip, and a retry while today's file is not published."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from finresearch.adapters import nse_fno_bhav as fb
from finresearch.adapters.http import PoliteClient
from finresearch.monitor import fno_bhav as job

UDIFF = (Path(__file__).parent / "fixtures" / "fno_bhav" / "udiff_20260930_sample.csv").read_text()
DAY = date(2026, 9, 30)  # a Wednesday
AFTER = datetime(2026, 9, 30, 14, 15, tzinfo=UTC)  # 19:45 IST
BEFORE = datetime(2026, 9, 30, 13, 45, tzinfo=UTC)  # 19:15 IST


def zipped(t: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("b.csv", t)
    return buf.getvalue()


async def _no_sleep(_s: float) -> None:
    return None


def test_due_slot_window_and_holidays():
    assert job.due_slot(BEFORE, holidays=set()) is None
    assert job.due_slot(AFTER, holidays=set()) == "fno_bhav:2026-09-30"
    assert job.due_slot(AFTER, holidays={DAY}) is None
    assert job.due_slot(datetime(2026, 10, 3, 15, 0, tzinfo=UTC), holidays=set()) is None  # Saturday


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE fno_bhav, fno_bhav_day, fno_iv_eod, fno_contract, alert_eval_slot"))
    return env


@respx.mock
async def test_step_retries_until_todays_file_is_published(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, FnoBhavDay

    published: list[bool] = []
    respx.get(fb.url_for(DAY, "udiff")).mock(
        side_effect=lambda _r: (
            httpx.Response(200, content=zipped(UDIFF)) if published else httpx.Response(404)
        )
    )
    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(return_value=httpx.Response(404))
    async with PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0) as http:
        res = await job.fno_bhav_step(AFTER, holidays=set(), http=http, stocks=[])
        assert res["fno_bhav"]["today_pending"] is True and res["fno_bhav"]["loaded"] == 0
        with session_scope() as s:
            slot = s.get(AlertEvalSlot, "fno_bhav:2026-09-30")
            assert slot.result["status"] == "failed" and slot.result["retry_at"]
        # 30 minutes later NSE has published: the retry loads today (and nothing else is downloaded twice)
        published.append(True)
        later = datetime(2026, 9, 30, 14, 50, tzinfo=UTC)
        res = await job.fno_bhav_step(later, holidays=set(), http=http, stocks=[], keep_raw=False)
        assert res["fno_bhav"]["today_pending"] is False and res["fno_bhav"]["loaded"] == 1
    with session_scope() as s:
        assert s.get(FnoBhavDay, DAY).status == "loaded"
        assert s.get(AlertEvalSlot, "fno_bhav:2026-09-30").result["status"] == "done"

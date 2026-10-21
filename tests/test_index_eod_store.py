"""Storing NSE's daily index close file (#366): idempotent day loads, the resumable backfill, the polite stops (two
refusals in a row, market hours) and the data-rights check. Test database only; NSE mocked with respx."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select, text

from finresearch.adapters import nse_index_eod as ie
from finresearch.adapters.http import PoliteClient

FX = Path(__file__).parent / "fixtures" / "index_eod"
CURRENT = (FX / "ind_close_all_current_synthetic.csv").read_text()
D = date(2022, 1, 3)  # a Monday
EVENING = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 19:30 IST, a Wednesday


def for_day(day: date) -> str:
    return CURRENT.replace("03-01-2022", day.strftime("%d-%m-%Y"))


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE index_eod, index_eod_day"))
    yield env
    with session_scope() as s:
        s.execute(text("TRUNCATE index_eod, index_eod_day"))


def count(model) -> int:
    from finresearch.db import session_scope

    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(model))


def test_load_day_is_idempotent(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import IndexEod, IndexEodDay
    from finresearch.refseries import index_eod as store

    for _ in range(2):
        with session_scope() as s:
            assert store.load_day(s, D, ie.url_for(D), CURRENT.encode()) == 7
    assert count(IndexEod) == 7
    with session_scope() as s:
        n = s.get(IndexEod, (D, "NIFTY"))
        assert float(n.close) == 17600.50 and float(n.div_yield) == 1.20 and n.index_name == "Nifty 50"
        assert s.get(IndexEod, (D, "INDIA VIX")).pe is None
        m = s.get(IndexEodDay, D)
        assert m.status == "loaded" and m.rows == 7 and m.attempts == 2


@respx.mock
async def test_backfill_resumes_and_skips_loaded_days(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import IndexEodDay
    from finresearch.refseries import index_eod as store

    fri, mon = date(2021, 12, 31), D
    route = respx.get(ie.url_for(mon)).mock(return_value=httpx.Response(200, text=for_day(mon)))
    respx.get(ie.url_for(fri)).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with client() as http:
        res = await store.backfill(fri, mon, today=date(2026, 10, 21), http=http, holidays=set(), keep_raw=False,
                                   now=lambda: EVENING)  # fmt: skip
        assert res["loaded"] == 1 and res["no_file"] == 1 and not res["failed"]
        assert respx.calls.call_count == 2  # the weekend between is not requested (no F&O session on record)
        again = await store.backfill(fri, mon, today=date(2026, 10, 21), http=http, holidays=set(),
                                     keep_raw=False, now=lambda: EVENING)  # fmt: skip
    assert again["planned"] == 0 and route.call_count == 1
    with session_scope() as s:
        assert s.get(IndexEodDay, fri).status == "no_file"


@respx.mock
async def test_two_refusals_in_a_row_stop_the_run(clean):
    from finresearch.refseries import index_eod as store

    respx.get(url__startswith=ie.ARCHIVES).mock(return_value=httpx.Response(403))
    async with client() as http:
        res = await store.backfill(date(2022, 1, 3), date(2022, 1, 14), today=date(2026, 10, 21), http=http,
                                   holidays=set(), keep_raw=False, now=lambda: EVENING)  # fmt: skip
    assert len(res["failed"]) == 2 and "refused" in res["stopped"]
    assert respx.calls.call_count == 2


async def test_a_backfill_does_not_run_in_market_hours(clean):
    from finresearch.refseries import index_eod as store

    market = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)  # 10:30 IST on a Wednesday
    res = await store.backfill(date(2022, 1, 3), date(2022, 1, 14), today=date(2026, 10, 21), holidays=set(),
                               keep_raw=False, now=lambda: market)  # fmt: skip
    assert res["loaded"] == 0 and "market hours" in res["stopped"]


def test_market_hours_window():
    from finresearch.refseries import index_eod as store

    assert store.in_market_hours(datetime(2026, 10, 21, 3, 30, tzinfo=UTC))  # 09:00 IST Wednesday
    assert not store.in_market_hours(datetime(2026, 10, 21, 10, 30, tzinfo=UTC))  # 16:00 IST
    assert not store.in_market_hours(datetime(2026, 10, 24, 5, 0, tzinfo=UTC))  # Saturday 10:30 IST


def test_every_nse_dataset_needs_a_recorded_decision():
    from finresearch import data_rights

    assert data_rights.require("index_eod").choice == "accept-personal"
    with pytest.raises(data_rights.NoDecision):
        data_rights.require("some_new_dataset")


def test_plan_uses_the_bhavcopy_sessions_for_weekends_and_holidays(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.refseries import index_eod as store

    sat_session, weekday_holiday = date(2024, 1, 20), date(2024, 1, 22)  # NSE traded on Sat 20-Jan-2024
    with session_scope() as s:
        s.add(FnoBhavDay(day=sat_session, status="loaded"))
        s.add(FnoBhavDay(day=weekday_holiday, status="no_file"))
    try:
        with session_scope() as s:
            days = store.plan_days(
                s, date(2024, 1, 19), date(2024, 1, 23), date(2026, 10, 21), holidays=set()
            )
        assert days == [date(2024, 1, 19), sat_session, date(2024, 1, 23)]
    finally:
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_bhav_day"))

"""Storing NSE's full equity bhavcopy (#373 (a)): idempotent day loads with lineage, the resumable backfill, the polite
stops (two refusals in a row, market hours), the data-rights check and the close lookups the peer build reads. Test
database only; NSE mocked with respx; the fixture is synthetic."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select, text

from finresearch.adapters import nse_equity_eod as ee
from finresearch.adapters.http import PoliteClient

FX = Path(__file__).parent / "fixtures" / "equity_eod"
FILE = (FX / "sec_bhavdata_full_synthetic.csv").read_text()
D = date(2024, 1, 15)  # a Monday
EVENING = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 19:30 IST, a Wednesday
TODAY = date(2026, 10, 21)


def for_day(day: date, text_: str = FILE) -> str:
    return text_.replace("15-Jan-2024", day.strftime("%d-%b-%Y"))


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE equity_eod, equity_eod_day, dataset_load"))
    yield env
    with session_scope() as s:
        s.execute(text("TRUNCATE equity_eod, equity_eod_day, dataset_load, fno_bhav_day"))


def count(model) -> int:
    from finresearch.db import session_scope

    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(model))


def test_load_day_is_idempotent_and_records_lineage(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import DatasetLoad, EquityEod, EquityEodDay
    from finresearch.refseries import equity_eod as store

    for _ in range(2):
        with session_scope() as s:
            assert store.load_day(s, D, ee.url_for(D), FILE.encode()) == 5
    assert count(EquityEod) == 5
    with session_scope() as s:
        a = s.get(EquityEod, (D, "ALPHACO", "EQ"))
        assert a.close == Decimal("104.00") and a.deliv_qty == 120000
        assert s.get(EquityEod, (D, "GAMMALTD", "BE")).deliv_pct is None
        m = s.get(EquityEodDay, D)
        assert m.status == "loaded" and m.rows == 5 and m.attempts == 2
        loads = s.scalars(select(DatasetLoad).where(DatasetLoad.dataset == "equity_eod")).all()
        assert (
            len(loads) == 2 and loads[0].url == ee.url_for(D) and loads[0].parser_version == ee.PARSER_VERSION
        )
        assert all(x.rows == 5 and x.prior_sha256 is None for x in loads)  # same body twice: no re-upload


def test_a_reupload_with_another_body_is_recorded(clean):
    from finresearch.db import session_scope
    from finresearch.dq.lineage import reuploads
    from finresearch.refseries import equity_eod as store

    with session_scope() as s:
        store.load_day(s, D, ee.url_for(D), FILE.encode())
    with session_scope() as s:
        store.load_day(s, D, ee.url_for(D), FILE.replace("104.00", "104.05").encode())
    with session_scope() as s:
        assert reuploads(s, "equity_eod", D) == 1


def test_close_prefers_eq_then_be_and_unknown_is_none(clean):
    from finresearch.db import session_scope
    from finresearch.refseries import equity_eod as store

    with session_scope() as s:
        store.load_day(s, D, ee.url_for(D), FILE.encode())
    with session_scope() as s:
        assert store.close_on(s, "ALPHACO", D) == (Decimal("104.00"), "EQ")  # not the N1 bond row
        assert store.close_on(s, "GAMMALTD", D) == (Decimal("21.93"), "BE")
        assert store.close_on(s, "NOSUCH", D) is None
        assert store.close_on(s, "ALPHACO", date(2024, 1, 16)) is None
        assert store.is_loaded(s, D) and not store.is_loaded(s, date(2024, 1, 16))


def test_closes_between_reads_one_series_per_day(clean):
    from finresearch.db import session_scope
    from finresearch.refseries import equity_eod as store

    tue = date(2024, 1, 16)
    with session_scope() as s:
        store.load_day(s, D, ee.url_for(D), FILE.encode())
        store.load_day(s, tue, ee.url_for(tue), for_day(tue).replace("104.00", "106.00").encode())
    with session_scope() as s:
        got = store.closes_between(s, "ALPHACO", date(2024, 1, 1), tue)
    assert got == [(D, Decimal("104.00")), (tue, Decimal("106.00"))]


@respx.mock
async def test_backfill_resumes_skips_loaded_days_and_keeps_raw(clean, tmp_path, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import EquityEodDay
    from finresearch.refseries import equity_eod as store

    monkeypatch.setattr(store, "raw_dir", lambda: tmp_path)
    fri, mon = date(2024, 1, 12), D
    route = respx.get(ee.url_for(mon)).mock(return_value=httpx.Response(200, text=for_day(mon)))
    respx.get(ee.url_for(fri)).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with client() as http:
        res = await store.backfill(fri, mon, today=TODAY, http=http, holidays=set(), now=lambda: EVENING)
        assert res["loaded"] == 1 and res["no_file"] == 1 and not res["failed"] and res["rows"] == 5
        assert respx.calls.call_count == 2  # the weekend between is not requested (no F&O session on record)
        assert store.raw_path(mon).is_file() and not store.raw_path(fri).exists()
        again = await store.backfill(fri, mon, today=TODAY, http=http, holidays=set(), now=lambda: EVENING)
    assert again["planned"] == 0 and route.call_count == 1
    with session_scope() as s:
        assert s.get(EquityEodDay, fri).status == "no_file"


async def test_a_kept_raw_file_is_reloaded_without_a_request(clean, tmp_path, monkeypatch):
    from finresearch.refseries import equity_eod as store

    monkeypatch.setattr(store, "raw_dir", lambda: tmp_path)
    store.raw_path(D).parent.mkdir(parents=True)
    store.raw_path(D).write_text(FILE)
    with respx.mock(assert_all_called=False) as mock:
        res = await store.backfill(D, D, today=TODAY, holidays=set(), now=lambda: EVENING)
    assert res["loaded"] == 1 and res["from_raw"] == 1 and mock.calls.call_count == 0


@respx.mock
async def test_two_refusals_in_a_row_stop_the_run(clean):
    from finresearch.refseries import equity_eod as store

    respx.get(url__startswith=ee.ARCHIVES).mock(return_value=httpx.Response(429))
    async with client() as http:
        res = await store.backfill(date(2024, 1, 15), date(2024, 1, 26), today=TODAY, http=http, holidays=set(),
                                   keep_raw=False, now=lambda: EVENING)  # fmt: skip
    assert len(res["failed"]) == 2 and "refused" in res["stopped"]
    assert respx.calls.call_count == 2


async def test_a_backfill_does_not_download_in_market_hours(clean):
    from finresearch.refseries import equity_eod as store

    market = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)  # 10:30 IST on a Wednesday
    with respx.mock(assert_all_called=False) as mock:
        res = await store.backfill(date(2024, 1, 15), date(2024, 1, 19), today=TODAY, holidays=set(),
                                   keep_raw=False, now=lambda: market)  # fmt: skip
    assert res["loaded"] == 0 and "market hours" in res["stopped"] and mock.calls.call_count == 0


async def test_no_data_rights_decision_no_request(clean, monkeypatch):
    from finresearch import data_rights
    from finresearch.refseries import equity_eod as store

    assert data_rights.require("equity_eod").choice == "accept-personal"
    monkeypatch.delitem(data_rights.DECISIONS, "equity_eod")
    with respx.mock(assert_all_called=False) as mock, pytest.raises(data_rights.NoDecision):
        await store.backfill(D, D, today=TODAY, holidays=set(), keep_raw=False, now=lambda: EVENING)
    assert mock.calls.call_count == 0


def test_plan_skips_known_holidays_and_weekends_without_a_session(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.refseries import equity_eod as store

    sat_session, weekday_holiday = date(2024, 1, 20), date(2024, 1, 22)  # NSE traded on Sat 20-Jan-2024
    with session_scope() as s:
        s.add(FnoBhavDay(day=sat_session, status="loaded"))
    with session_scope() as s:
        days = store.plan_days(s, date(2024, 1, 19), date(2024, 1, 23), TODAY, holidays={weekday_holiday})
    assert days == [date(2024, 1, 19), sat_session, date(2024, 1, 23)]

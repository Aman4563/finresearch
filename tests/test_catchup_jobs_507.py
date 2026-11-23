"""Catch-up after an outage (#507) in the nightly jobs themselves: which days each job asks NSE for after three
weeks down. NSE is mocked with respx and answers 404 for every day, so the days asked for are the test. Calendar
(2026): 30-Oct is a Friday, 2-Nov a Monday, 23-Nov a Monday; expected day lists are written out by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select, text

from finresearch.adapters import nse_equity_eod as ee
from finresearch.adapters import nse_index_eod as ie
from finresearch.adapters import nse_participant_oi as po
from finresearch.adapters import nse_span as sp
from finresearch.adapters.http import PoliteClient

D = date
TODAY = D(2026, 11, 23)  # a Monday
EVENING = datetime(2026, 11, 23, 14, 15, tzinfo=UTC)  # 19:45 IST: after every job's RUN_AFTER
MORNING = datetime(2026, 11, 23, 4, 30, tzinfo=UTC)  # 10:00 IST: market hours
NOV_WEEKDAYS = [D(2026, 11, n) for n in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 23)]
NSE = "https://nsearchives.nseindia.com/"


async def _no_sleep(_s: float) -> None:
    return None


def _http() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


def _asked(url_for) -> list[date]:
    """The days whose file was requested, oldest first (searched over two years around the test's dates)."""
    by_url = {
        url_for(D(2026, 1, 1) + timedelta(days=n)): D(2026, 1, 1) + timedelta(days=n) for n in range(365)
    }
    return sorted({by_url[str(c.request.url)] for c in respx.calls if str(c.request.url) in by_url})


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE equity_eod, equity_eod_day, index_eod, index_eod_day, fno_bhav, fno_bhav_day, "
                           "fno_iv_eod, fno_contract, span_param, span_day, fo_volt, fno_participant_oi, "
                           "fno_participant_vol, fno_participant_day, dataset_load"))  # fmt: skip
            s.execute(text("DELETE FROM alert_eval_slot WHERE slot ~ '^(equity_eod|index_eod|fno_bhav|span|"
                           "fno_participant|catchup_gap):'"))  # fmt: skip
            s.execute(text("DELETE FROM alert WHERE kind = 'catchup_gap'"))

    wipe()
    yield env
    wipe()


def _loaded(model, *days: date) -> None:
    from finresearch.db import session_scope

    with session_scope() as s:
        for d in days:
            s.add(model(day=d, status="loaded", attempts=1))


def _nightly_ran(slot: str) -> None:
    """The record a nightly job leaves of an evening it ran (alert_eval_slot)."""
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot

    with session_scope() as s:
        s.add(AlertEvalSlot(slot=slot, result={"status": "done", "attempts": 1}))


# ---------------------------------------------------------------------------------------------- equity (4 days)
@respx.mock
async def test_equity_job_refills_three_weeks_and_skips_holidays_and_weekends(clean):
    from finresearch.db.models import EquityEodDay
    from finresearch.monitor import equity_eod as job

    _loaded(EquityEodDay, D(2026, 10, 29), D(2026, 10, 30))
    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404, text="<html></html>"))
    holiday = D(2026, 11, 10)
    async with _http() as http:
        res = await job.equity_eod_step(EVENING, holidays={holiday}, http=http, keep_raw=False, pause=0.0)
    # every weekday from 2-Nov to 23-Nov except the holiday; before #507 only 19, 20 and 23-Nov were asked for
    assert _asked(ee.url_for) == [d for d in NOV_WEEKDAYS if d != holiday]
    assert res["equity_eod"]["catch_up"] == {"from": "2026-10-31", "days": 19}


@respx.mock
async def test_equity_job_is_bounded_and_reports_the_older_gap_once(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, EquityEodDay
    from finresearch.monitor import equity_eod as job

    _loaded(EquityEodDay, D(2026, 9, 1))  # a Tuesday, 83 days back
    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with _http() as http:
        res = await job.equity_eod_step(EVENING, holidays=set(), http=http, keep_raw=False, pause=0.0)
        # 45 days back from 23-Nov is Friday 9-Oct: 1 + 5 + 5 + 5 October weekdays, then November's 16
        asked = _asked(ee.url_for)
        assert asked[0] == D(2026, 10, 9) and len(asked) == 16 + 16
        gap = {"from": "2026-09-02", "to": "2026-10-08", "sessions": 27}
        assert res["equity_eod"]["catch_up"] == {"from": "2026-10-09", "days": 41, "uncovered": gap}
        # the next evening the same gap is still there (nothing loaded): logged again, but not a second alert
        await job.equity_eod_step(
            EVENING + timedelta(days=1), holidays=set(), http=http, keep_raw=False, pause=0.0
        )
    with session_scope() as s:
        alerts = s.scalars(select(Alert).where(Alert.kind == "catchup_gap")).all()
        assert len(alerts) == 1 and alerts[0].level == "warn"
        assert "27 session(s) from 02-Sep-2026 to 08-Oct-2026" in alerts[0].message
        assert "finresearch equity backfill --from 2026-09-02 --to 2026-10-08" in alerts[0].message


@respx.mock
async def test_equity_job_fills_a_hole_and_retries_a_failed_day_older_than_its_lookback(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import EquityEodDay
    from finresearch.monitor import equity_eod as job

    # the outage was half filled by an older build: 16-Nov to 20-Nov loaded, 2-Nov to 13-Nov never asked for,
    # except Thursday 5-Nov which failed
    _loaded(EquityEodDay, D(2026, 10, 30), *[D(2026, 11, n) for n in (16, 17, 18, 19, 20)])
    with session_scope() as s:
        s.add(EquityEodDay(day=D(2026, 11, 5), status="failed", attempts=1, error="NseError: HTTP 503"))
    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with _http() as http:
        await job.equity_eod_step(EVENING, holidays=set(), http=http, keep_raw=False, pause=0.0)
    assert _asked(ee.url_for) == [D(2026, 11, n) for n in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 23)]


@respx.mock
async def test_days_before_the_stores_first_loaded_day_are_history_not_a_gap(clean):
    from finresearch.db.models import EquityEodDay
    from finresearch.monitor import equity_eod as job

    _loaded(
        EquityEodDay, *[D(2026, 11, n) for n in (12, 13, 16, 17, 18, 19, 20)]
    )  # the store began on 12-Nov
    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with _http() as http:
        res = await job.equity_eod_step(EVENING, holidays=set(), http=http, keep_raw=False)
    assert _asked(ee.url_for) == [TODAY] and "catch_up" not in res["equity_eod"]


@respx.mock
async def test_an_empty_store_keeps_the_normal_lookback(clean):
    from finresearch.monitor import equity_eod as job

    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with _http() as http:
        res = await job.equity_eod_step(EVENING, holidays=set(), http=http, keep_raw=False)
    assert (
        _asked(ee.url_for) == [D(2026, 11, 19), D(2026, 11, 20), TODAY]
        and "catch_up" not in res["equity_eod"]
    )


@respx.mock
async def test_no_catch_up_download_in_market_hours(clean):
    from finresearch.db.models import EquityEodDay, FnoBhavDay, IndexEodDay, SpanDay
    from finresearch.monitor import equity_eod, fno_bhav, index_eod, span

    for model in (EquityEodDay, IndexEodDay, FnoBhavDay, SpanDay):
        _loaded(model, D(2026, 10, 30))  # three weeks behind
    route = respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404))
    async with _http() as http:
        for step in (equity_eod.equity_eod_step, index_eod.index_eod_step, fno_bhav.fno_bhav_step,
                     fno_bhav.fno_participant_step, span.span_step):  # fmt: skip
            assert await step(MORNING, holidays=set(), http=http) == {}  # 10:00 IST on a trading day: not due
    assert route.call_count == 0


# ---------------------------------------------------------------------------------------------- the other jobs
@respx.mock
async def test_index_job_refills_three_weeks(clean):
    from finresearch.db.models import IndexEodDay
    from finresearch.monitor import index_eod as job

    _loaded(IndexEodDay, D(2026, 10, 30))
    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with _http() as http:
        res = await job.index_eod_step(EVENING, holidays=set(), http=http, keep_raw=False, pause=0.0)
    assert _asked(ie.url_for) == NOV_WEEKDAYS  # before #507: 19, 20 and 23-Nov
    assert res["index_eod"]["catch_up"] == {"from": "2026-10-31", "days": 19}


@respx.mock
async def test_fno_bhavcopy_job_refills_three_weeks_and_probes_the_weekends(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.monitor import fno_bhav as job

    _loaded(FnoBhavDay, D(2026, 10, 30))
    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404))
    async with _http() as http:
        res = await job.fno_bhav_step(
            EVENING, holidays=set(), http=http, stocks=[], keep_raw=False, pause=0.0
        )
    with session_scope() as s:
        tried = sorted(s.scalars(select(FnoBhavDay.day).where(FnoBhavDay.status == "no_file")))
    # every calendar day from Saturday 31-Oct to 23-Nov (the F&O store probes weekends); before #507 from 13-Nov
    assert tried == [D(2026, 10, 31) + timedelta(days=n) for n in range(24)]
    assert res["fno_bhav"]["catch_up"] == {"from": "2026-10-31", "days": 13}  # 13 days before 13-Nov


@respx.mock
async def test_span_job_has_its_own_smaller_bound(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, SpanDay
    from finresearch.monitor import span as job

    _loaded(SpanDay, D(2026, 10, 30))
    _nightly_ran("span:2026-10-30")
    respx.get(url__startswith=sp.ARCHIVES).mock(return_value=httpx.Response(404, text="<html/>"))
    async with _http() as http:
        res = await job.span_step(EVENING, holidays=set(), http=http, pause=0.0)
    # 14 days back from 23-Nov is Monday 9-Nov; the week of 2-Nov (5 sessions) is reported, not fetched (~10 MB each)
    assert _asked(sp.url_for) == [D(2026, 11, n) for n in (9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 23)]
    gap = {"from": "2026-11-02", "to": "2026-11-06", "sessions": 5}
    assert res["span"]["catch_up"] == {"from": "2026-11-09", "days": 10, "uncovered": gap}
    with session_scope() as s:
        msg = s.scalars(select(Alert.message).where(Alert.kind == "catchup_gap")).one()
    assert "finresearch fno span backfill --daily --from 2026-11-02 --to 2026-11-06" in msg


@respx.mock
async def test_participant_job_starts_from_the_file_that_is_further_behind(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoParticipantOi, FnoParticipantVol
    from finresearch.monitor import fno_bhav as job

    zeros = {k: 0 for k in po.KEYS}
    with session_scope() as s:  # open interest is up to date, the volume file stopped on 30-Oct
        for d in (D(2026, 10, 30), *[x for x in NOV_WEEKDAYS if x != TODAY]):
            s.add(FnoParticipantOi(day=d, client_type="FII", source="t", **zeros))
        s.add(FnoParticipantVol(day=D(2026, 10, 30), client_type="FII", source="t", **zeros))
    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404))
    async with _http() as http:
        res = await job.fno_participant_step(EVENING, holidays=set(), http=http, pause=0.0)
    assert _asked(po.VOL.url_for) == NOV_WEEKDAYS  # before #507: 18, 19, 20 and 23-Nov
    assert _asked(po.OI.url_for) == [TODAY]
    assert res["fno_participant"]["catch_up"] == {"from": "2026-10-31", "days": 18}


# ---------------------------------------------------------------------------------------------- older than the bound
@respx.mock
async def test_a_hole_older_than_spans_bound_is_reported_when_the_newest_days_are_loaded(clean):
    """What exists at deploy: SPAN loaded to 30-Oct, an older build then loaded 19, 20 and 23-Nov. On Tuesday 24-Nov
    the 14-day window starts at 10-Nov; 2-Nov to 9-Nov is out of reach and must be said, not dropped."""
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, SpanDay
    from finresearch.monitor import span as job

    _loaded(SpanDay, D(2026, 10, 30), D(2026, 11, 19), D(2026, 11, 20), D(2026, 11, 23))
    _nightly_ran("span:2026-10-30")
    respx.get(url__startswith=sp.ARCHIVES).mock(return_value=httpx.Response(404, text="<html/>"))
    async with _http() as http:
        res = await job.span_step(EVENING + timedelta(days=1), holidays=set(), http=http, pause=0.0)
    assert _asked(sp.url_for) == [D(2026, 11, n) for n in (10, 11, 12, 13, 16, 17, 18, 24)]
    gap = {"from": "2026-11-02", "to": "2026-11-09", "sessions": 6}  # 2, 3, 4, 5, 6 and Monday 9-Nov
    assert res["span"]["catch_up"] == {"from": "2026-11-10", "days": 10, "uncovered": gap}
    with session_scope() as s:
        msg = s.scalars(select(Alert.message).where(Alert.kind == "catchup_gap")).one()
    assert "finresearch fno span backfill --daily --from 2026-11-02 --to 2026-11-09" in msg


@respx.mock
async def test_spans_monthly_samples_from_before_the_nightly_job_are_not_a_gap(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, SpanDay
    from finresearch.monitor import span as job

    # one sampled file a month until the nightly job began on 30-Oct (fnohist.span), every session since
    _loaded(
        SpanDay, D(2026, 9, 15), D(2026, 10, 15), D(2026, 10, 30), *[d for d in NOV_WEEKDAYS if d != TODAY]
    )
    _nightly_ran("span:2026-10-30")
    respx.get(url__startswith=sp.ARCHIVES).mock(return_value=httpx.Response(404, text="<html/>"))
    async with _http() as http:
        res = await job.span_step(EVENING, holidays=set(), http=http)
    assert _asked(sp.url_for) == [TODAY] and "catch_up" not in res["span"]
    with session_scope() as s:
        assert s.scalars(select(Alert).where(Alert.kind == "catchup_gap")).all() == []


@respx.mock
async def test_the_first_ever_span_night_does_not_backfill_from_the_last_sample(clean):
    from finresearch.db.models import SpanDay
    from finresearch.monitor import span as job

    _loaded(SpanDay, D(2026, 10, 15))  # a sampled file; tonight is the nightly job's first run
    respx.get(url__startswith=sp.ARCHIVES).mock(return_value=httpx.Response(404, text="<html/>"))
    async with _http() as http:
        res = await job.span_step(EVENING, holidays=set(), http=http)
    assert _asked(sp.url_for) == [D(2026, 11, 19), D(2026, 11, 20), TODAY] and "catch_up" not in res["span"]


@respx.mock
async def test_an_old_hole_in_the_equity_store_is_reported_once_and_not_fetched(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, EquityEodDay
    from finresearch.monitor import equity_eod as job

    # every weekday from 1-Sep to 20-Nov is loaded except the week of 14-Sep (older than the 9-Oct floor)
    days = [D(2026, 9, 1) + timedelta(days=n) for n in range(81)]
    hole = {D(2026, 9, n) for n in (14, 15, 16, 17, 18)}
    _loaded(EquityEodDay, *[d for d in days if d.weekday() < 5 and d not in hole])
    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with _http() as http:
        res = await job.equity_eod_step(EVENING, holidays=set(), http=http, keep_raw=False)
        await job.equity_eod_step(EVENING + timedelta(days=1), holidays=set(), http=http, keep_raw=False)
    assert _asked(ee.url_for) == [TODAY, D(2026, 11, 24)]  # the hole is never downloaded by the nightly job
    gap = {"from": "2026-09-14", "to": "2026-09-18", "sessions": 5}
    assert res["equity_eod"]["catch_up"] == {"from": "2026-11-19", "days": 0, "uncovered": gap}
    with session_scope() as s:
        (msg,) = s.scalars(select(Alert.message).where(Alert.kind == "catchup_gap")).all()
    assert "finresearch equity backfill --from 2026-09-14 --to 2026-09-18" in msg


@respx.mock
async def test_a_fault_in_the_catch_up_never_costs_tonights_file(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, EquityEodDay
    from finresearch.monitor import catchup
    from finresearch.monitor import equity_eod as job

    def broken(*_a, **_kw):
        raise RuntimeError("plan broke")

    _loaded(EquityEodDay, D(2026, 10, 30))
    monkeypatch.setattr(catchup, "for_store", broken)
    respx.get(url__startswith=NSE).mock(return_value=httpx.Response(404, text="<html></html>"))
    async with _http() as http:
        res = await job.equity_eod_step(EVENING, holidays=set(), http=http, keep_raw=False)
    assert _asked(ee.url_for) == [D(2026, 11, 19), D(2026, 11, 20), TODAY]  # the normal 4 days still ran
    # and the result says the gaps are unknown, not that nothing was missed
    assert res["equity_eod"]["catch_up"] == {
        "from": "2026-11-19",
        "days": 0,
        "error": "RuntimeError: plan broke",
    }
    with session_scope() as s:
        assert (
            s.get(AlertEvalSlot, "equity_eod:2026-11-23").result["catch_up"]["error"]
            == "RuntimeError: plan broke"
        )

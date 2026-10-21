"""Positioning history (#371): NSE's participant-wise OI and trading-volume files, the resumable backfill from 2015,
the nightly load of both files, lineage and the data-quality checks. Synthetic files only (made-up counts in NSE's
layout: a title line, padded header names, Client/DII/FII/Pro/TOTAL); no network: respx answers every request."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from finresearch.adapters import nse_participant_oi as po
from finresearch.adapters.http import PoliteClient

HEADER = ("Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short       ,Option Index "
          "Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,"
          "Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts      ,"
          "Total Short Contracts")  # fmt: skip
# made-up counts per participant in the 12 bucket columns, in the header's order (futures long, short; then options
# call long, put long, call short, put short): each long column's total equals its short column's total
LONGS, SHORTS = (0, 2, 4, 5, 8, 9), (1, 3, 6, 7, 10, 11)
BUCKETS = {
    "Client": (100, 60, 300, 200, 5000, 4000, 4800, 4100, 700, 300, 650, 320),
    "DII": (10, 40, 120, 260, 5, 6, 3, 2, 9, 1, 30, 4),
    "FII": (40, 70, 280, 230, 900, 1100, 1000, 1050, 250, 140, 240, 130),
    "Pro": (50, 30, 200, 210, 6000, 6500, 6102, 6454, 900, 500, 939, 487),
}
EVENING = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 19:30 IST, a Wednesday
TODAY = date(2026, 10, 21)
D = date(2015, 1, 5)  # a Monday


def synth(
    kind: po.Kind, day: date, buckets: dict[str, tuple[int, ...]] = BUCKETS, *, compact: bool = False
) -> str:
    """A participant file in NSE's layout. compact=True prints the date as 'Jan 05,2015' (NSE's 2022 files)."""
    when = day.strftime("%b %d,%Y") if compact else day.strftime("%b %d, %Y")
    phrase = "Open Interest" if kind is po.OI else "Trading Volume"
    lines = [f'""Participant wise {phrase} (no. of contracts) in Equity Derivatives as on {when}"",,,,,,,,,,,,,,',
             HEADER]  # fmt: skip
    for who, b in buckets.items():
        lines.append(
            ",".join([who, *map(str, b), str(sum(b[i] for i in LONGS)), str(sum(b[i] for i in SHORTS))])
        )
    tot = [sum(b[i] for b in buckets.values()) for i in range(12)]
    lines.append(
        ",".join(["TOTAL", *map(str, tot), str(sum(tot[i] for i in LONGS)), str(sum(tot[i] for i in SHORTS))])
    )
    return "\n".join(lines) + "\n"


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


# ------------------------------------------------------------------------------------------------------- adapter
def test_volume_file_parses_like_the_oi_file():
    got = po.parse(synth(po.VOL, D), po.VOL)
    assert got.day == D and set(got.rows) == {"Client", "DII", "FII", "Pro"}
    assert got.rows["FII"]["fut_idx_long"] == 40 and got.rows["Pro"]["opt_idx_put_short"] == 6454
    # Total Long = the sum of the six long columns as written: 900 + 1100 + 40 + 280 + 250 + 140
    assert got.rows["FII"]["total_long"] == 2710 and not got.notes


def test_a_file_of_the_other_kind_is_refused():
    with pytest.raises(po.ParticipantFileError, match="Trading Volume"):
        po.parse(synth(po.OI, D), po.VOL)
    with pytest.raises(po.ParticipantFileError, match="Open Interest"):
        po.parse(synth(po.VOL, D), po.OI)


def test_compact_title_date_is_read():
    assert po.parse(synth(po.VOL, date(2022, 1, 3), compact=True), po.VOL).day == date(2022, 1, 3)


def test_urls_per_kind():
    assert (
        po.VOL.url_for(D) == "https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_05012015.csv"
    )
    assert (
        po.OI.url_for(D) == "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_05012015.csv"
    )
    assert {k.dataset for k in po.KINDS} == {"fno_participant_oi", "fno_participant_vol"}


@respx.mock
async def test_fetch_returns_the_served_bytes_and_refuses_block_pages():
    body = synth(po.VOL, D).encode()
    respx.get(po.VOL.url_for(D)).mock(return_value=httpx.Response(200, content=body))
    respx.get(po.VOL.url_for(date(2015, 1, 6))).mock(
        return_value=httpx.Response(200, text="<html>Denied</html>")
    )
    respx.get(po.VOL.url_for(date(2015, 1, 7))).mock(return_value=httpx.Response(404))
    async with client() as http:
        assert await po.fetch_day(http, D, po.VOL) == (po.VOL.url_for(D), body)
        with pytest.raises(po.NseError, match="block page"):
            await po.fetch_day(http, date(2015, 1, 6), po.VOL)
        assert await po.fetch_day(http, date(2015, 1, 7), po.VOL) is None


# ----------------------------------------------------------------------------------------------- store / backfill
@pytest.fixture
def clean(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    sql = "TRUNCATE fno_participant_oi, fno_participant_vol, fno_participant_day, dataset_load, fno_bhav_day"
    with session_scope() as s:
        s.execute(text(sql))
    yield env
    with session_scope() as s:
        s.execute(text(sql))


def serve(days: list[date], kinds=po.KINDS) -> None:
    for k in kinds:
        for d in days:
            respx.get(k.url_for(d)).mock(return_value=httpx.Response(200, content=synth(k, d).encode()))


async def run(start: date, end: date, **kw):
    from finresearch.fnohist import participants

    kw = {"today": TODAY, "holidays": set(), "keep_raw": False, "now": lambda: EVENING} | kw
    async with client() as http:
        return await participants.backfill(start, end, http=http, **kw)


@respx.mock
async def test_backfill_loads_both_files_with_lineage(clean):
    import hashlib

    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import DatasetLoad, FnoParticipantDay, FnoParticipantOi, FnoParticipantVol

    serve([D])
    res = await run(D, D)
    assert res["fno_participant_oi"]["loaded"] == 1 and res["fno_participant_vol"]["loaded"] == 1
    with session_scope() as s:
        assert s.get(FnoParticipantVol, (D, "FII")).opt_idx_put_short == 1050
        assert s.get(FnoParticipantOi, (D, "Pro")).fut_idx_long == 50
        m = s.get(FnoParticipantDay, ("fno_participant_vol", D))
        assert m.status == "loaded" and m.rows == 4 and m.url == po.VOL.url_for(D)
        loads = {r.dataset: r for r in s.scalars(select(DatasetLoad))}
    assert set(loads) == {"fno_participant_oi", "fno_participant_vol"}
    assert loads["fno_participant_vol"].sha256 == hashlib.sha256(synth(po.VOL, D).encode()).hexdigest()
    assert (
        loads["fno_participant_vol"].rows == 4
        and loads["fno_participant_vol"].parser_version == po.PARSER_VERSION
    )


@respx.mock
async def test_backfill_resumes_and_skips_days_already_stored(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoParticipantDay
    from finresearch.fnohist import participants

    mon, tue, wed = D, date(2015, 1, 6), date(2015, 1, 7)
    with (
        session_scope() as s
    ):  # an OI day stored before the manifest existed (#317's nightly job): not re-fetched
        participants.save_day(s, wed, po.parse(synth(po.OI, wed)), po.OI.url_for(wed))
    serve([mon])
    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(return_value=httpx.Response(404))
    res = await run(mon, wed)
    assert res["fno_participant_oi"]["planned"] == 2 and res["fno_participant_vol"]["planned"] == 3
    assert res["fno_participant_oi"]["no_file"] == 1 and res["fno_participant_vol"]["no_file"] == 2
    calls = respx.calls.call_count
    again = await run(mon, wed)  # loaded and old no_file days are skipped
    assert again["fno_participant_oi"]["planned"] == 0 and again["fno_participant_vol"]["planned"] == 0
    assert respx.calls.call_count == calls
    with session_scope() as s:
        assert s.get(FnoParticipantDay, ("fno_participant_vol", tue)).status == "no_file"


@respx.mock
async def test_two_refusals_stop_only_that_dataset(clean):
    days = [date(2015, 1, d) for d in (5, 6, 7, 8, 9)]
    serve(days, kinds=(po.OI,))
    vol = respx.get(url__startswith=po.VOL.url.split("{")[0]).mock(return_value=httpx.Response(403))
    res = await run(days[0], days[-1])
    assert (
        "refused" in res["fno_participant_vol"]["stopped"] and len(res["fno_participant_vol"]["failed"]) == 2
    )
    assert vol.call_count == 2  # nothing more is asked of a dataset NSE refused
    assert res["fno_participant_oi"]["loaded"] == 5 and "stopped" not in res["fno_participant_oi"]


@respx.mock
async def test_block_pages_are_failures_not_missing_days(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoParticipantDay

    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(
        return_value=httpx.Response(200, text="<html>Access Denied</html>")
    )
    res = await run(D, date(2015, 1, 9))
    for k in po.KINDS:
        assert res[k.dataset]["no_file"] == 0 and len(res[k.dataset]["failed"]) == 2
        assert "stopped" in res[k.dataset]
    with session_scope() as s:
        assert s.get(FnoParticipantDay, ("fno_participant_oi", D)).status == "failed"


async def test_no_download_in_market_hours(clean):
    market = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)  # 10:30 IST on a Wednesday
    with respx.mock(assert_all_called=False) as mock:
        res = await run(D, date(2015, 1, 9), now=lambda: market)
        assert mock.calls.call_count == 0
    assert "market hours" in res["stopped"]
    assert res["fno_participant_oi"]["loaded"] == 0


async def test_no_data_rights_decision_no_request(clean, monkeypatch):
    from finresearch import data_rights

    monkeypatch.delitem(data_rights.DECISIONS, "fno_participant_vol")
    with respx.mock(assert_all_called=False) as mock, pytest.raises(data_rights.NoDecision):
        await run(D, D)
    assert mock.calls.call_count == 0


def test_plan_follows_the_bhavcopy_sessions_and_holidays(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.fnohist import participants

    sat, sun, hol, bhav_hol = date(2024, 1, 20), date(2024, 1, 21), date(2024, 1, 26), date(2024, 1, 22)
    with session_scope() as s:
        s.add(FnoBhavDay(day=sat, status="loaded"))  # NSE traded on Saturday 20-Jan-2024
        s.add(FnoBhavDay(day=bhav_hol, status="no_file"))
    with session_scope() as s:
        days = participants.plan_days(s, po.VOL, sat, date(2024, 1, 26), TODAY, holidays={hol})
    assert sat in days and sun not in days and bhav_hol not in days and hol not in days
    assert days == [sat, date(2024, 1, 23), date(2024, 1, 24), date(2024, 1, 25)]


# ---------------------------------------------------------------------------------------------------- nightly job
@respx.mock
async def test_nightly_step_loads_both_files_and_retries_a_missing_volume_file(clean):
    from sqlalchemy import text

    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, FnoParticipantVol
    from finresearch.monitor import fno_bhav as job

    day = date(2026, 10, 9)  # a Friday
    now = datetime(2026, 10, 9, 14, 15, tzinfo=UTC)  # 19:45 IST
    serve([day], kinds=(po.OI,))
    vol = respx.get(po.VOL.url_for(day)).mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(return_value=httpx.Response(404))
    with session_scope() as s:
        s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'fno_participant:%'"))
    async with client() as http:
        res = await job.fno_participant_step(now, holidays=set(), http=http)
        assert res["fno_participant"]["loaded"] == 1
        with session_scope() as s:  # the volume file is not out yet: the slot fails and is retried
            assert s.get(AlertEvalSlot, "fno_participant:2026-10-09").result["status"] == "failed"
        vol.mock(return_value=httpx.Response(200, content=synth(po.VOL, day).encode()))
        res = await job.fno_participant_step(now + timedelta(hours=1), holidays=set(), http=http)
    assert res["fno_participant"]["loaded"] == 1
    with session_scope() as s:
        assert s.get(AlertEvalSlot, "fno_participant:2026-10-09").result["status"] == "done"
        assert s.get(FnoParticipantVol, (day, "FII")) is not None


# --------------------------------------------------------------------------------------------------- data quality
def _checks(fn, day):
    from finresearch.db import session_scope

    with session_scope() as s:
        return {c.check: c for c in fn(s, day)}


def test_volume_checks_present_identity_and_reupload(clean):
    from finresearch.db import session_scope
    from finresearch.dq import ref
    from finresearch.fnohist import participants

    assert {c.status for c in _checks(ref.participant_vol_checks, D).values()} == {"unknown"}  # not loaded
    body = synth(po.VOL, D).encode()
    with session_scope() as s:
        participants.load_day(s, po.VOL, D, po.VOL.url_for(D), body)
    got = _checks(ref.participant_vol_checks, D)
    assert {k: c.status for k, c in got.items()} == {"participants_present": "ok", "long_short_identity": "ok",
                                                     "reupload": "ok"}  # fmt: skip
    assert all(c.dataset == "fno_participant_vol" for c in got.values())
    # NSE re-uploads the day with one more Client contract on each side: same totals, another body
    changed = synth(po.VOL, D, BUCKETS | {"Client": (101, 61, *BUCKETS["Client"][2:])}).encode()
    with session_scope() as s:
        participants.load_day(s, po.VOL, D, po.VOL.url_for(D), changed)
    rep = _checks(ref.participant_vol_checks, D)["reupload"]
    assert rep.status == "warn" and rep.value == 1 and rep.critical is False


def test_oi_rows_without_lineage_have_an_unknown_reupload_check(clean):
    from finresearch.db import session_scope
    from finresearch.dq import ref
    from finresearch.fnohist import participants

    with session_scope() as s:  # stored by #317's nightly job before lineage existed
        participants.save_day(s, D, po.parse(synth(po.OI, D)), po.OI.url_for(D))
    rep = _checks(ref.participant_checks, D)["reupload"]
    assert rep.status == "unknown" and rep.critical is False


def test_volume_is_a_monitored_dataset():
    from finresearch.dq import run

    ds = {d.name: d for d in run.DATASETS}["fno_participant_vol"]
    assert ds.critical is False and ds.sla.hour == 20

"""FII derivative statistics (#371): NSE's daily fii_stats .xls, the resumable backfill, the nightly slot, lineage and
the data-quality checks. Synthetic workbooks only, written in each test with xlwt (made-up numbers in the layout the
adapter expects); no network: respx answers every request."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
import xlwt

from finresearch.adapters import nse_fii_stats as fs
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NseError

D = date(2020, 10, 9)  # a Friday
# made-up (buy contracts, buy amt, sell contracts, sell amt, OI contracts, OI amt) per category
ROWS = {
    "INDEX FUTURES": (41_234, 3_210.55, 39_876, 3_101.2, 160_500, 12_480.75),
    "INDEX OPTIONS": (1_900_321, 150_210.1, 1_880_002, 148_900.95, 950_000, 75_300.0),
    "STOCK FUTURES": (90_120, 6_010.4, 88_765, 5_920.35, 1_120_400, 74_890.6),
    "STOCK OPTIONS": (120_045, 8_100.25, 119_800, 8_090.0, 140_200, 9_450.3),
}


def synth_xls(day: date = D, rows: dict[str, tuple] = ROWS, *, title: str | None = None,
              header: tuple[str, ...] = ("BUY", "SELL", "OPEN INTEREST AT THE END OF THE DAY"),
              notes: bool = True) -> bytes:  # fmt: skip
    """An OLE2 .xls in the expected layout: a title row, a BUY/SELL/OI header over two columns each, a sub-header
    (No. of contracts / Amt in Crores), one row per category, then free-text notes."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    ws.write(0, 0, title if title is not None else f"FII DERIVATIVES STATISTICS FOR {day:%d-%b-%Y}")
    for i, h in enumerate(header):
        ws.write(2, 1 + 2 * i, h)
    for i in range(3):
        ws.write(3, 1 + 2 * i, "No. of contracts")
        ws.write(3, 2 + 2 * i, "Amt in Crores")
    for r, (label, vals) in enumerate(rows.items(), start=4):
        ws.write(r, 0, label)
        for c, v in enumerate(vals, start=1):
            if v is not None:
                ws.write(r, c, v)
    if notes:
        ws.write(5 + len(rows), 0, "Note: synthetic workbook for tests; the numbers are made up")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


# ------------------------------------------------------------------------------------------------------- adapter
def test_url_names_the_day_as_dd_mon_yyyy():
    assert fs.url_for(D) == "https://nsearchives.nseindia.com/content/fo/fii_stats_09-Oct-2020.xls"


def test_parse_reads_every_category_with_whole_contracts_and_decimal_amounts():
    got = fs.parse(synth_xls())
    assert got.day == D and list(got.rows) == list(ROWS)
    fut = got.rows["INDEX FUTURES"]
    assert fut == {"buy_contracts": 41_234, "buy_amt_cr": Decimal("3210.55"), "sell_contracts": 39_876,
                   "sell_amt_cr": Decimal("3101.2"), "oi_contracts": 160_500, "oi_amt_cr": Decimal("12480.75")}  # fmt: skip
    assert isinstance(fut["buy_contracts"], int)


def test_extra_categories_are_kept_and_labels_normalised():
    rows = {**ROWS, "  nifty   futures ": (1, 0.5, 2, 1.0, 3, 1.5)}
    assert fs.parse(synth_xls(rows=rows)).rows["NIFTY FUTURES"]["oi_contracts"] == 3


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"title": "SOMETHING ELSE"}, "title"),
        ({"header": ("BUY", "SELL", "TURNOVER")}, "header"),
        ({"rows": {k: v for k, v in ROWS.items() if k != "STOCK OPTIONS"}}, "STOCK OPTIONS"),
        ({"rows": {**ROWS, "INDEX FUTURES": (1.5, 1, 1, 1, 1, 1)}}, "whole"),
        ({"rows": {**ROWS, "INDEX FUTURES": (-1, 1, 1, 1, 1, 1)}}, "negative"),
        ({"rows": {**ROWS, "INDEX FUTURES": (1, -0.5, 1, 1, 1, 1)}}, "negative"),
        ({"rows": {**ROWS, "INDEX FUTURES": (1, 1, None, 1, 1, 1)}}, "INDEX FUTURES"),
        ({"rows": {**ROWS, "INDEX FUTURES": (1, 1, "n/a", 1, 1, 1)}}, "INDEX FUTURES"),
        ({"rows": {**ROWS, "X" * 41: (1, 1, 1, 1, 1, 1)}}, "longer"),  # fits the table's 40 or is refused
        ({"rows": {**ROWS, "index  futures": (1, 1, 1, 1, 1, 1)}}, "twice"),
    ],
)
def test_parse_refuses_a_layout_it_does_not_know(change, match):
    with pytest.raises(fs.FiiStatsError, match=match):
        fs.parse(synth_xls(**change))


def test_parse_refuses_a_non_workbook():
    with pytest.raises(fs.FiiStatsError, match="workbook"):
        fs.parse(b"<html>Access Denied</html>")


@respx.mock
async def test_fetch_day_returns_the_body_as_served_and_none_on_404():
    body = synth_xls()
    respx.get(fs.url_for(D)).mock(return_value=httpx.Response(200, content=body))
    respx.get(fs.url_for(date(2020, 10, 12))).mock(return_value=httpx.Response(404))
    async with client() as http:
        assert await fs.fetch_day(http, D) == (fs.url_for(D), body)
        assert await fs.fetch_day(http, date(2020, 10, 12)) is None


@respx.mock
@pytest.mark.parametrize(
    "resp", [httpx.Response(200, text="<html>Access Denied</html>"), httpx.Response(403)]
)
async def test_fetch_day_raises_on_a_block_page_or_refusal(resp):
    respx.get(fs.url_for(D)).mock(return_value=resp)
    async with client() as http:
        with pytest.raises(NseError):
            await fs.fetch_day(http, D)


# ----------------------------------------------------------------------------------------------- store / backfill
EVENING = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 19:30 IST, a Wednesday
TODAY = date(2026, 10, 21)
WEEK = [date(2020, 10, d) for d in (5, 6, 7, 8, 9)]  # Monday to Friday


@pytest.fixture
def clean(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    sql = "TRUNCATE fii_stats, fno_participant_day, dataset_load, fno_bhav_day"
    with session_scope() as s:
        s.execute(text(sql))
    yield env
    with session_scope() as s:
        s.execute(text(sql))


@pytest.fixture
def undecided(monkeypatch):
    """No recorded decision, whatever the owner's record says: the gate itself is under test."""
    from finresearch import data_rights

    monkeypatch.delitem(data_rights.DECISIONS, fs.DATASET, raising=False)


@pytest.fixture
def decided(monkeypatch):
    """A recorded decision for the test only, whatever the owner's record says (accepted on 23-Oct-2026, #481)."""
    from finresearch import data_rights

    monkeypatch.setitem(data_rights.DECISIONS, fs.DATASET, data_rights.Decision(
        fs.DATASET, "accept-personal", "2026-10-23", "test only", "test only"))  # fmt: skip


def serve(days: list[date]) -> None:
    for d in days:
        respx.get(fs.url_for(d)).mock(return_value=httpx.Response(200, content=synth_xls(d)))


async def run(start: date, end: date, **kw):
    from finresearch.fnohist import fii_stats

    kw = {"today": TODAY, "holidays": set(), "keep_raw": False, "now": lambda: EVENING} | kw
    async with client() as http:
        return await fii_stats.backfill(start, end, http=http, **kw)


def test_the_owner_accepted_the_dataset_on_23_oct():
    # PENDING (#371/#473) until the owner's decision of 23-Oct-2026 (#481): accept-personal under the #362 terms
    from finresearch import data_rights

    d = data_rights.require(fs.DATASET)
    assert (d.choice, d.decided, d.ref) == ("accept-personal", "2026-10-23", "issue #481")
    assert fs.DATASET not in data_rights.PENDING


def test_without_a_decision_the_dataset_is_refused(undecided):
    from finresearch import data_rights

    with pytest.raises(data_rights.NoDecision, match="fii_stats"):
        data_rights.require(fs.DATASET)


async def test_no_decision_no_request(clean, undecided):
    from finresearch import data_rights

    with respx.mock(assert_all_called=False) as mock, pytest.raises(data_rights.NoDecision):
        await run(D, D)
    assert mock.calls.call_count == 0


@respx.mock
async def test_backfill_stores_rows_manifest_and_lineage(clean, decided):
    import hashlib

    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import DatasetLoad, FiiStats, FnoParticipantDay

    serve([D])
    res = await run(D, D)
    assert res[fs.DATASET]["loaded"] == 1
    with session_scope() as s:
        r = s.get(FiiStats, (D, "INDEX OPTIONS"))
        assert (r.oi_contracts, r.sell_amt_cr, r.source) == (950_000, Decimal("148900.95"), fs.url_for(D))
        assert len(s.scalars(select(FiiStats)).all()) == 4
        m = s.get(FnoParticipantDay, (fs.DATASET, D))
        assert m.status == "loaded" and m.rows == 4 and m.url == fs.url_for(D)
        load = s.scalars(select(DatasetLoad).where(DatasetLoad.dataset == fs.DATASET)).one()
        assert (
            load.sha256 == hashlib.sha256(synth_xls(D)).hexdigest()
            and load.parser_version == fs.PARSER_VERSION
        )


async def test_a_file_dated_another_day_is_refused(clean):
    from finresearch.db import session_scope
    from finresearch.fnohist import fii_stats

    with pytest.raises(fs.FiiStatsError, match="2020-10-08"), session_scope() as s:
        fii_stats.load_day(s, date(2020, 10, 8), fs.url_for(date(2020, 10, 8)), synth_xls(D))


@respx.mock
async def test_backfill_resumes_and_skips_weekends_and_loaded_days(clean, decided):
    serve(WEEK[:2])
    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(return_value=httpx.Response(404))
    res = await run(WEEK[0], date(2020, 10, 11))  # Mon..Sun: the weekend is not asked for
    assert (res[fs.DATASET]["planned"], res[fs.DATASET]["loaded"], res[fs.DATASET]["no_file"]) == (5, 2, 3)
    calls = respx.calls.call_count
    again = await run(WEEK[0], date(2020, 10, 11))
    assert again[fs.DATASET]["planned"] == 0 and respx.calls.call_count == calls


@respx.mock
async def test_two_refusals_stop_the_dataset(clean, decided):
    route = respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(
        return_value=httpx.Response(429)
    )
    res = await run(WEEK[0], WEEK[-1])
    assert "refused" in res[fs.DATASET]["stopped"] and route.call_count == 2


@respx.mock
async def test_block_pages_are_failures_not_missing_days(clean, decided):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoParticipantDay

    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(
        return_value=httpx.Response(200, text="<html>Access Denied</html>"))  # fmt: skip
    res = await run(WEEK[0], WEEK[-1])
    assert (
        res[fs.DATASET]["no_file"] == 0
        and len(res[fs.DATASET]["failed"]) == 2
        and "stopped" in res[fs.DATASET]
    )
    with session_scope() as s:
        assert s.get(FnoParticipantDay, (fs.DATASET, WEEK[0])).status == "failed"


async def test_no_download_in_market_hours(clean, decided):
    market = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)  # 10:30 IST on a Wednesday
    with respx.mock(assert_all_called=False) as mock:
        res = await run(WEEK[0], WEEK[-1], now=lambda: market)
    assert mock.calls.call_count == 0 and "market hours" in res["stopped"]


@respx.mock
async def test_raw_files_are_kept_and_reread(clean, decided, tmp_path, monkeypatch):
    from finresearch.fnohist import fii_stats, participants

    monkeypatch.setattr(participants, "raw_dir", lambda: tmp_path)
    serve([D])
    await run(D, D, keep_raw=True)
    assert (tmp_path / "2020" / "fii_stats_09-Oct-2020.xls").read_bytes() == synth_xls(D)
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:  # forget the load: the next run re-reads the kept file without a request
        s.execute(text("TRUNCATE fii_stats, fno_participant_day"))
    calls = respx.calls.call_count
    res = await run(D, D, keep_raw=True)
    assert (
        res[fs.DATASET]["from_raw"] == 1
        and res[fs.DATASET]["loaded"] == 1
        and respx.calls.call_count == calls
    )
    assert fii_stats.SOURCE.dataset == fs.DATASET


# ---------------------------------------------------------------------------------------------------- nightly job
NIGHT_DAY = date(2026, 10, 9)  # a Friday
NIGHT = datetime(2026, 10, 9, 14, 15, tzinfo=UTC)  # 19:45 IST


def _slot():
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot

    with session_scope() as s:
        r = s.get(AlertEvalSlot, f"fii_stats:{NIGHT_DAY}")
        return None if r is None else r.result


@pytest.fixture
def no_slot(clean):
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'fii_stats:%'"))
    return clean


async def test_nightly_without_a_decision_is_skipped_not_failed(no_slot, undecided):
    from finresearch.monitor import fno_bhav as job

    with respx.mock(assert_all_called=False) as mock:
        async with client() as http:
            res = await job.fii_stats_step(NIGHT, holidays=set(), http=http)
    assert mock.calls.call_count == 0 and "fii_stats" in res["fii_stats"]["skipped"]
    assert _slot()["status"] == "done"  # no retry: nothing will change until the owner decides


@respx.mock
async def test_nightly_loads_today_and_retries_until_it_is_out(no_slot, decided):
    from finresearch.monitor import fno_bhav as job

    today = respx.get(fs.url_for(NIGHT_DAY)).mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(return_value=httpx.Response(404))
    async with client() as http:
        await job.fii_stats_step(NIGHT, holidays=set(), http=http)
        assert _slot()["status"] == "failed"  # not published yet: retried
        today.mock(return_value=httpx.Response(200, content=synth_xls(NIGHT_DAY)))
        res = await job.fii_stats_step(NIGHT + timedelta(hours=1), holidays=set(), http=http)
    assert res["fii_stats"]["loaded"] == 1 and _slot()["status"] == "done"


# --------------------------------------------------------------------------------------------------- data quality
def _checks(day):
    from finresearch.db import session_scope
    from finresearch.dq import ref

    with session_scope() as s:
        return {c.check: c for c in ref.fii_stats_checks(s, day)}


def _load(day: date, rows: dict[str, tuple] = ROWS) -> None:
    from finresearch.db import session_scope
    from finresearch.fnohist import fii_stats

    with session_scope() as s:
        fii_stats.load_day(s, day, fs.url_for(day), synth_xls(day, rows))


def test_dq_checks_categories_values_and_reupload(clean):
    assert {c.status for c in _checks(D).values()} == {"unknown"}  # not loaded: unknown, never ok
    _load(D)
    got = _checks(D)
    assert {k: c.status for k, c in got.items()} == {"categories_present": "ok", "value_identity": "ok",
                                                     "reupload": "ok"}  # fmt: skip
    assert all(c.dataset == "fii_stats" and c.critical is False for c in got.values())
    _load(D, {**ROWS, "INDEX FUTURES": (41_235, *ROWS["INDEX FUTURES"][1:])})  # NSE re-uploads the day
    assert _checks(D)["reupload"].status == "warn"


def test_dq_flags_value_without_contracts(clean):
    # a value with no contracts cannot happen (the reverse can: a few cheap option contracts round to 0.00 crore)
    _load(D, {**ROWS, "STOCK OPTIONS": (0, 12.5, 119_800, 8_090.0, 140_200, 9_450.3)})
    c = _checks(D)["value_identity"]
    assert c.status == "fail" and c.value == 1 and "STOCK OPTIONS buy" in c.note


def test_dq_flags_a_missing_category(clean):
    from sqlalchemy import delete

    from finresearch.db import session_scope
    from finresearch.db.models import FiiStats

    _load(D)
    with session_scope() as s:
        s.execute(delete(FiiStats).where(FiiStats.category == "STOCK FUTURES"))
    c = _checks(D)["categories_present"]
    assert c.status == "fail" and "STOCK FUTURES" in c.note


def test_fii_stats_is_a_monitored_non_critical_dataset():
    from finresearch.dq import report, run

    ds = {d.name: d for d in run.DATASETS}["fii_stats"]
    assert ds.critical is False and ds.sla.hour == 20
    assert report.LABELS["fii_stats"] == "FII derivative statistics"


# ------------------------------------------------------------------------------------------------------------ CLI
def test_cli_passes_dates_and_delay(monkeypatch):
    from typer.testing import CliRunner

    from finresearch.cli import app
    from finresearch.fnohist import fii_stats

    seen = {}

    async def fake(start, end, **kw):
        seen.update(start=start, end=end, **kw)
        return {"fii_stats": {"planned": 0}}

    monkeypatch.setattr(fii_stats, "backfill", fake)
    r = CliRunner().invoke(
        app, ["fno", "fii-stats", "--from", "2015-01-01", "--to", "2015-12-31", "--delay", "3"]
    )
    assert r.exit_code == 0, r.output
    assert (seen["start"], seen["end"], seen["pause"], seen["keep_raw"]) == (date(2015, 1, 1), date(2015, 12, 31),
                                                                             3.0, True)  # fmt: skip


def test_cli_without_a_decision_says_so_and_exits_2(undecided):
    from typer.testing import CliRunner

    from finresearch.cli import app

    with respx.mock(assert_all_called=False) as mock:
        r = CliRunner().invoke(app, ["fno", "fii-stats", "--from", "2015-01-01", "--to", "2015-01-02"])
    assert r.exit_code == 2 and "fii_stats" in r.output and mock.calls.call_count == 0

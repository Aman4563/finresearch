"""Storing NSE's F&O bhavcopy (#306): idempotent day loads, the resumable backfill, holiday skips, derived IV rows
and the contract calendar. Test database only; NSE is mocked with respx (fixtures cut from NSE's public files)."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from finresearch.adapters import nse_fno_bhav as fb
from finresearch.adapters.http import PoliteClient

FX = Path(__file__).parent / "fixtures" / "fno_bhav"
UDIFF = (FX / "udiff_20260930_sample.csv").read_text()
LEGACY = (FX / "legacy_fo05JUL2024_sample.csv").read_text()
D_UDIFF, D_LEGACY = date(2026, 9, 30), date(2024, 7, 5)


def zipped(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bhav.csv", text)
    return buf.getvalue()


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


@pytest.fixture
def clean(env):
    """The test database is shared by the session: start every test with empty F&O history tables."""
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE fno_bhav, fno_bhav_day, fno_iv_eod, fno_contract"))
    yield env
    with session_scope() as s:  # the database is shared by the session: leave nothing for other tests
        s.execute(text("TRUNCATE fno_bhav, fno_bhav_day, fno_iv_eod, fno_contract"))


def count(model) -> int:
    from finresearch.db import session_scope

    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(model))


def test_load_day_is_idempotent_and_drops_dead_contracts(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay
    from finresearch.fnohist import store

    for _ in range(2):
        with session_scope() as s:
            st = store.load_day(s, D_UDIFF, "udiff", "u", zipped(UDIFF), ["RELIANCE"])
    # NIFTYNXT50 options are not kept (not in INDEX_OPTIONS), its futures are; its Dec-2026 future had no OI and no
    # trade (close 70493.00 stale, settle 70847.61 theoretical), so it is counted dead, not stored
    assert st == {"total": 306, "kept": 302, "dead": 1}
    assert count(FnoBhav) == 302
    with session_scope() as s:
        m = s.get(FnoBhavDay, D_UDIFF)
        assert (m.status, m.format, m.rows_kept, m.attempts, m.stock_symbols) == (
            "loaded",
            "udiff",
            302,
            2,
            ["RELIANCE"],
        )
        assert (
            s.scalar(select(func.count()).where(FnoBhav.symbol == "NIFTYNXT50", FnoBhav.instrument == "IDO"))
            == 0
        )
    # without RELIANCE: its 28 options go, its futures stay (every future is kept)
    with session_scope() as s:
        assert store.load_day(s, D_UDIFF, "udiff", "u", zipped(UDIFF), [])["kept"] == 274


def test_dead_contract_is_counted_not_stored(clean):
    from finresearch.db import session_scope
    from finresearch.fnohist import store

    with session_scope() as s:
        st = store.load_day(s, D_LEGACY, "legacy", "l", zipped(LEGACY), [])
    # the NIFTY 11-Jul-2024 21100 CE row: no trade, no OI, a theoretical settle of 3248.5
    assert st["dead"] == 1 and st["kept"] == 117 - 1 - 9  # TCS options are not tracked here


def test_wrong_trade_date_is_refused(clean):
    from finresearch.db import session_scope
    from finresearch.fnohist import store

    with session_scope() as s, pytest.raises(fb.NseError):
        store.load_day(s, date(2026, 10, 1), "udiff", "u", zipped(UDIFF), [])


def test_plan_skips_known_weekday_holidays_but_probes_weekends(clean):
    from finresearch.db import session_scope
    from finresearch.fnohist import store

    with session_scope() as s:
        days = store.plan_days(s, date(2026, 9, 28), date(2026, 10, 4), date(2026, 10, 12), [],
                               holidays={date(2026, 10, 2)})  # fmt: skip
    # Gandhi Jayanti (Friday 2-Oct-2026) is skipped; Saturday/Sunday are probed (NSE has held weekend sessions)
    assert days == [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 3),
                    date(2026, 10, 4)]  # fmt: skip


@respx.mock
async def test_backfill_resumes_reloads_from_raw_and_retries_failures(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay
    from finresearch.fnohist import store

    legacy = respx.get(fb.url_for(D_LEGACY, "legacy")).mock(
        return_value=httpx.Response(200, content=zipped(LEGACY))
    )
    respx.get(fb.url_for(D_LEGACY, "udiff")).mock(return_value=httpx.Response(404))
    for d in (date(2024, 7, 6), date(2024, 7, 7)):  # a weekend: both routes 404
        respx.get(fb.url_for(d, "udiff")).mock(return_value=httpx.Response(404))
        respx.get(fb.url_for(d, "legacy")).mock(return_value=httpx.Response(404))
    refused = respx.get(fb.url_for(date(2024, 7, 8), "udiff")).mock(return_value=httpx.Response(403))
    kw = {"today": date(2024, 7, 20), "holidays": set(), "derive": False}
    async with client() as http:
        r1 = await store.backfill(D_LEGACY, date(2024, 7, 8), stocks=[], http=http, **kw)
        assert (r1["planned"], r1["loaded"], r1["no_file"], list(r1["failed"])) == (4, 1, 2, ["2024-07-08"])
        assert legacy.call_count == 1 and refused.call_count == 1
        n = count(FnoBhav)
        # second run: only the failed day is planned again; loaded and old no_file days cost nothing
        r2 = await store.backfill(D_LEGACY, date(2024, 7, 8), stocks=[], http=http, **kw)
        assert (
            r2["planned"] == 1 and legacy.call_count == 1 and refused.call_count == 2 and count(FnoBhav) == n
        )
        # a newly tracked stock re-loads the loaded day from the kept zip, without a download
        r3 = await store.backfill(D_LEGACY, date(2024, 7, 8), stocks=["TCS"], http=http, **kw)
        assert r3["from_raw"] == 1 and legacy.call_count == 1 and count(FnoBhav) == n + 9
    with session_scope() as s:
        failed = s.get(FnoBhavDay, date(2024, 7, 8))
        assert failed.status == "failed" and "403" in failed.error and failed.attempts == 3


def test_derive_day_on_a_real_day(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod
    from finresearch.fnohist import derive, store

    with session_scope() as s:
        store.load_day(s, D_UDIFF, "udiff", "u", zipped(UDIFF), ["RELIANCE"])
        assert derive.derive_day(s, D_UDIFF) == 2  # NIFTY and RELIANCE have options that day
    with session_scope() as s:
        nifty = s.scalar(select(FnoIvEod).where(FnoIvEod.symbol == "NIFTY"))
        # 6-Oct is 6 days away (< 7), so near = 13-Oct (no future: put-call parity) and next = 27-Oct (its future)
        assert (nifty.near_expiry, nifty.next_expiry, nifty.forward_source) == (date(2026, 10, 13), date(2026, 10, 27),
                                                                                "parity")  # fmt: skip
        assert (
            5 < float(nifty.near_iv) < 60
            and 5 < float(nifty.iv_30d) < 60
            and nifty.iv_30d_method == "interpolated"
        )
        assert (
            float(nifty.underlying) == 22620.45 and nifty.rv_20 is None
        )  # one day of futures: no 20-day window
        assert abs(float(nifty.forward) - 22620.45) < 200


def test_contract_calendar_monthly_weekly_and_lots(clean):
    from finresearch.db import session_scope
    from finresearch.fnohist import derive, store

    with session_scope() as s:
        store.load_day(s, D_UDIFF, "udiff", "u", zipped(UDIFF), [])
        store.load_day(s, D_LEGACY, "legacy", "l", zipped(LEGACY), [])
        derive.rebuild_contracts(s)
    with session_scope() as s:
        c = {x.expiry: x for x in derive.contracts(s, "NIFTY")}
        assert (c[date(2026, 10, 27)].kind, c[date(2026, 10, 13)].kind) == ("monthly", "weekly")
        assert (c[date(2026, 10, 27)].lot_size, c[date(2026, 10, 27)].lot_source) == (65, "bhavcopy")
        jul = c[
            date(2024, 7, 11)
        ]  # legacy: no lot in the file; NIFTY's lot was 25 then (OI in multiples of 25)
        assert jul.kind == "weekly" and jul.underlying_kind == "index"
        assert (jul.lot_size, jul.lot_source, jul.lot_changes) == (
            25,
            "inferred_oi_gcd",
            None,
        )  # 51 OI figures


def test_infer_lot_needs_enough_points():
    from finresearch.fnohist.derive import infer_lot

    assert infer_lot([25 * k for k in (3, 7, 10, 11, 13, 17, 19, 23, 29, 31)]) == 25
    assert infer_lot([50, 100, 150]) is None  # too few: 50 could be two lots of 25


@respx.mock
async def test_a_derivation_error_keeps_the_day_loaded(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay
    from finresearch.fnohist import derive, store

    def boom(s, day):
        raise ZeroDivisionError("odd day")

    monkeypatch.setattr(derive, "derive_day", boom)
    respx.get(fb.url_for(D_UDIFF, "udiff")).mock(return_value=httpx.Response(200, content=zipped(UDIFF)))
    async with client() as http:
        res = await store.backfill(D_UDIFF, D_UDIFF, today=date(2026, 10, 12), stocks=[], http=http, holidays=set(),
                                   keep_raw=False)  # fmt: skip
    assert res["loaded"] == 1 and res["failed"] == {} and "2026-09-30" in res["derive_failed"]
    assert count(FnoBhav) == 274
    with session_scope() as s:
        m = s.get(FnoBhavDay, D_UDIFF)
        assert m.status == "loaded" and m.error.startswith("derive: ZeroDivisionError")

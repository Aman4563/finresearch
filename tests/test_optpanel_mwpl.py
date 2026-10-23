"""The MWPL utilisation backfill, the derived ban history and its check against NSE's dated notices (P1.12, #374).
Test database only; NSE mocked with respx; synthetic symbols. The data-rights decision is the owner's: without it
nothing is requested."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from optpanel_synth import zipped
from sqlalchemy import func, select, text

from finresearch import data_rights
from finresearch.adapters import nse_mwpl as mw
from finresearch.adapters.http import IST, PoliteClient

FX = Path(__file__).parent / "fixtures" / "nse" / "mwpl"
OI_CSV = (FX / "combineoi_02032026_synthetic.csv").read_text()
D1, D2, D3 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)
EVENING = datetime(2026, 3, 5, 20, 0, tzinfo=IST)
TABLES = "mwpl_oi, fno_ban_notice, optpanel_file_day, dataset_load, fno_bhav_day"


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
        for d in (D1, D2, D3):
            s.add(FnoBhavDay(day=d, status="loaded"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))


@pytest.fixture
def decided(monkeypatch):
    for k in ("nse_combineoi", "nse_secban"):
        monkeypatch.setitem(data_rights.DECISIONS, k, data_rights.Decision(k, "accept-personal", "2026-10-22",
                                                                           "test", "test"))  # fmt: skip


def oi_for(day: date) -> str:
    return OI_CSV.replace("02-Mar-2026", f"{day:%d-%b-%Y}")


def test_both_new_datasets_carry_the_owners_decision() -> None:
    # PENDING until the owner's decision of 23-Oct-2026 (#471): accept-personal under #362's safeguards
    for k in ("nse_combineoi", "nse_secban"):
        assert k not in data_rights.PENDING
        d = data_rights.require(k)
        assert (d.choice, d.decided, d.ref) == ("accept-personal", "2026-10-23", "issue #471")


async def test_backfill_refuses_without_a_decision_and_requests_nothing(clean, monkeypatch) -> None:
    from finresearch.optpanel import mwpl

    monkeypatch.delitem(data_rights.DECISIONS, "nse_combineoi")
    async with respx.mock(assert_all_mocked=True) as m:
        with pytest.raises(data_rights.NoDecision):
            await mwpl.backfill(D1, D3, today=D3, http=client(), now=lambda: EVENING)
        assert m.calls.call_count == 0


async def test_backfill_loads_sessions_keeps_raw_records_lineage_and_resumes(clean, decided) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import DatasetLoad, MwplOi, OptPanelFileDay
    from finresearch.optpanel import mwpl

    bodies = {d: zipped(oi_for(d)) for d in (D1, D3)}
    async with respx.mock() as m:
        for d, b in bodies.items():
            m.get(mw.combineoi_url(d)).mock(return_value=httpx.Response(200, content=b))
        m.get(mw.combineoi_url(D2)).mock(return_value=httpx.Response(404, text="nf"))
        out = await mwpl.backfill(D1, D3, today=date(2026, 3, 20), http=client(), now=lambda: EVENING)
        assert (out["loaded"], out["no_file"], out["failed"]) == (2, 1, {})
        again = await mwpl.backfill(D1, D3, today=date(2026, 3, 20), http=client(), now=lambda: EVENING)
        assert (
            again["planned"] == 0 and m.calls.call_count == 3
        )  # loaded and old no_file days are not re-asked
    with session_scope() as s:
        a = s.get(MwplOi, (D1, "ZZALPHA"))
        assert (a.mwpl, a.oi, a.utilisation, a.oi_basis) == (
            1_000_000,
            960_000,
            Decimal("0.96"),
            "Open Interest",
        )
        assert s.get(OptPanelFileDay, ("nse_combineoi", D2)).status == "no_file"
        lin = s.scalars(
            select(DatasetLoad).where(DatasetLoad.dataset == "nse_combineoi", DatasetLoad.day == D1)
        ).one()
        assert lin.sha256 == hashlib.sha256(bodies[D1]).hexdigest() and lin.rows == 3
    assert mwpl.raw_path(D1).read_bytes() == bodies[D1]


async def test_backfill_makes_no_download_in_market_hours_but_reads_kept_files(clean, decided) -> None:
    from finresearch.optpanel import mwpl

    p = mwpl.raw_path(D1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(zipped(oi_for(D1)))
    morning = datetime(2026, 3, 5, 10, 0, tzinfo=IST)
    async with respx.mock(assert_all_mocked=True) as m:
        out = await mwpl.backfill(D1, D3, today=D3, http=client(), now=lambda: morning)
        assert m.calls.call_count == 0
    assert out["loaded"] == 1 and "market hours" in out["stopped"]


async def test_backfill_stops_after_repeated_refusals(clean, decided) -> None:
    from finresearch.optpanel import mwpl

    async with respx.mock() as m:
        m.get(url__regex=r".*combineoi_.*").mock(return_value=httpx.Response(403, text="denied"))
        out = await mwpl.backfill(D1, D3, today=D3, http=client(), now=lambda: EVENING)
        assert m.calls.call_count == 2
    assert "403/429" in out["stopped"] and len(out["failed"]) == 2


def seed_mwpl(rows: dict[date, dict[str, float]]) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import MwplOi, OptPanelFileDay

    with session_scope() as s:
        for d, us in rows.items():
            s.add(OptPanelFileDay(dataset="nse_combineoi", day=d, status="loaded"))
            for sym, u in us.items():
                s.add(MwplOi(day=d, symbol=sym, mwpl=1000, oi=int(u * 1000), utilisation=Decimal(str(u)),
                             oi_basis="Open Interest"))  # fmt: skip


def test_ban_history_is_keyed_by_the_next_session_and_forgets_after_a_gap(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.optpanel import mwpl

    d4 = date(2026, 3, 5)
    with session_scope() as s:
        s.add(FnoBhavDay(day=d4, status="loaded"))
    seed_mwpl(
        {D1: {"ZZALPHA": 0.97, "ZZBETA": 0.5}, D3: {"ZZALPHA": 0.85, "ZZBETA": 0.85}, d4: {"ZZBETA": 0.4}}
    )
    with session_scope() as s:
        h = mwpl.ban_history(s, D1, d4)
    # D1's file decides D2; D2 has no file, so D3 is unknown for everyone; D3's 0.85 cannot decide D4; ZZALPHA is
    # absent from D4's file (unknown), and D4's file decides a session not yet in the calendar (left out)
    assert h[D2] == {"ZZALPHA": True, "ZZBETA": False}
    assert h[D3] == {"ZZALPHA": None, "ZZBETA": None}
    assert h[d4] == {"ZZALPHA": None, "ZZBETA": None}
    assert set(h) == {D2, D3, d4}


async def test_check_notices_compares_derived_bans_and_stores_the_notices(clean, decided) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import DatasetLoad, FnoBanNotice
    from finresearch.optpanel import mwpl

    seed_mwpl({D1: {"ZZALPHA": 0.97, "ZZBETA": 0.5}, D2: {"ZZALPHA": 0.90, "ZZBETA": 0.96}})
    async with respx.mock() as m:
        m.get(mw.secban_url(D2)).mock(return_value=httpx.Response(200, text=(
            "Securities in Ban For Trade Date 03-MAR-2026:\n1,ZZALPHA\n")))  # fmt: skip
        m.get(mw.secban_url(D3)).mock(return_value=httpx.Response(200, text=(
            "Securities in Ban For Trade Date 04-MAR-2026:\n1,ZZALPHA\n")))  # fmt: skip
        out = await mwpl.check_notices(n=20, seed=1, http=client(), now=lambda: EVENING)
    assert out["sampled"] == 2 and out["matched"] == 1
    by = {r["trade_day"]: r for r in out["days"]}
    assert by[D2.isoformat()]["match"] is True
    assert by[D3.isoformat()]["extra"] == ["ZZBETA"] and by[D3.isoformat()]["match"] is False
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(FnoBanNotice)) == 2
        assert (
            s.scalar(select(func.count()).select_from(DatasetLoad).where(DatasetLoad.dataset == "nse_secban"))
            == 2
        )


async def test_check_notices_requests_nothing_in_market_hours(clean, decided) -> None:
    from finresearch.optpanel import mwpl

    seed_mwpl({D1: {"ZZALPHA": 0.97}})
    async with respx.mock(assert_all_mocked=True) as m:
        out = await mwpl.check_notices(
            n=20, http=client(), now=lambda: datetime(2026, 3, 5, 11, 0, tzinfo=IST)
        )
        assert m.calls.call_count == 0
    assert "market hours" in out["stopped"]

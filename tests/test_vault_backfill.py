"""Loading the sealed 2008-2021 vault (#365, P1.3): only into the vault database, only vault-period days, only NIFTY /
BANKNIFTY / FINNIFTY index futures and options, politely (outside market hours, stop on refusals), resumable.
NSE is mocked with respx; the fixture is a synthetic legacy file (made-up prices, never NSE data)."""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select, text

from finresearch.adapters import nse_fno_bhav as fb
from finresearch.adapters.http import PoliteClient
from finresearch.fincalc.dates import IST

FX = Path(__file__).parent / "fixtures" / "vault" / "legacy_synthetic_14OCT2008.csv"
DAY = date(2008, 10, 14)  # a Tuesday
OTHER_APP = "postgresql+psycopg://localhost/finresearch_not_the_vault_test"
EVENING = datetime(2026, 10, 22, 21, 0, tzinfo=IST)


def zipped(t: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bhav.csv", t)
    return buf.getvalue()


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


@pytest.fixture
def vault(env, db_url):
    """The test database plays the vault; tables start and end empty."""
    from finresearch.db import session_scope

    def wipe() -> None:
        with session_scope(db_url) as s:
            s.execute(text("TRUNCATE fno_bhav, fno_bhav_day, fno_iv_eod, fno_contract, dataset_load"))

    wipe()
    yield db_url
    wipe()


def kw(**extra):
    return {"app_url": OTHER_APP, "now": lambda: EVENING, "pause": 0.0, "holidays": set(), **extra}


async def test_refuses_without_a_vault_database(vault):
    from finresearch.vault import backfill as vb

    with pytest.raises(vb.VaultLoadRefused, match="no vault database"):
        await vb.backfill(DAY, DAY, **kw(vault_url=None))


async def test_refuses_the_app_database(vault):
    from finresearch.vault import backfill as vb

    with pytest.raises(vb.VaultLoadRefused, match="app's database"):
        await vb.backfill(DAY, DAY, **kw(vault_url=vault, app_url=vault))


@pytest.mark.parametrize(("start", "end"), [(date(2007, 12, 31), date(2008, 1, 2)), (date(2021, 12, 31), date(2022, 1, 3)),
                                            (date(2009, 1, 5), date(2009, 1, 2))])  # fmt: skip
async def test_refuses_days_outside_the_vault(vault, start, end):
    from finresearch.vault import backfill as vb

    with pytest.raises(vb.VaultLoadRefused, match="vault period|after"):
        await vb.backfill(start, end, **kw(vault_url=vault))


def test_plan_skips_weekends_and_known_holidays_but_probes_listed_weekend_sessions(vault):
    from finresearch.db import session_scope
    from finresearch.vault import backfill as vb

    with session_scope(vault) as s:
        days = vb.plan_days(s, date(2015, 2, 23), date(2015, 3, 3), holidays={date(2015, 2, 17)},
                            extra_days={date(2015, 2, 28)})  # fmt: skip
    # Mon 23 .. Fri 27 Feb, Sat 28-Feb (listed: Budget day session), not Sun 1-Mar, then Mon 2, Tue 3 Mar
    assert days == [date(2015, 2, 23), date(2015, 2, 24), date(2015, 2, 25), date(2015, 2, 26), date(2015, 2, 27),
                    date(2015, 2, 28), date(2015, 3, 2), date(2015, 3, 3)]  # fmt: skip
    with session_scope(vault) as s:
        assert date(2015, 2, 17) not in vb.plan_days(s, date(2015, 2, 16), date(2015, 2, 18), holidays={date(2015, 2, 17)})


def test_load_keeps_only_the_vaults_index_contracts(vault):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay
    from finresearch.fnohist import store
    from finresearch.vault import backfill as vb

    with session_scope(vault) as s:
        st = store.load_day(s, DAY, "legacy", "l", zipped(FX.read_text()), [], keep=vb.keep)
    # 14 rows: NIFTY 2 futures + 4 live options (+1 dead 4500 CE), BANKNIFTY future + option; MINIFTY, CNXIT and
    # ACMECORP rows are outside the vault
    assert st == {"total": 14, "kept": 8, "dead": 1}
    with session_scope(vault) as s:
        syms = set(s.scalars(select(FnoBhav.symbol).distinct()))
        kinds = set(s.scalars(select(FnoBhav.instrument).distinct()))
        m = s.get(FnoBhavDay, DAY)
        assert syms == {"NIFTY", "BANKNIFTY"} and kinds == {"IDF", "IDO"}
        assert (m.status, m.rows_total, m.rows_kept, m.rows_dead) == ("loaded", 14, 8, 1)


@respx.mock
async def test_backfill_writes_only_to_the_vault_and_resumes(vault, monkeypatch):
    from finresearch import config, db
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay, FnoContract, FnoIvEod
    from finresearch.vault import backfill as vb

    # the app's database is unreachable here: any write that missed the vault URL would fail the run
    monkeypatch.setenv("FINRESEARCH_DATABASE_URL", "postgresql+psycopg://localhost:1/nowhere_test")
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    file = respx.get(fb.url_for(DAY, "legacy")).mock(return_value=httpx.Response(200, content=zipped(FX.read_text())))
    holiday = respx.get(fb.url_for(date(2008, 10, 15), "legacy")).mock(return_value=httpx.Response(404))
    async with client() as http:
        r = await vb.backfill(DAY, date(2008, 10, 19), vault_url=vault, http=http, **kw(app_url=None))
        # Tue 14 (file), Wed 15 (404), Thu 16 / Fri 17 not mocked: respx refuses them (failed); Sat/Sun not requested
        assert (r["planned"], r["loaded"], r["no_file"], sorted(r["failed"])) == (4, 1, 1, ["2008-10-16", "2008-10-17"])
        assert r["contracts"] == 3, r  # NIFTY Oct + Nov, BANKNIFTY Oct
        assert "derive_failed" not in r, r
        r2 = await vb.backfill(DAY, date(2008, 10, 19), vault_url=vault, http=http, **kw(app_url=None))
        assert r2["planned"] == 2 and file.call_count == 1 and holiday.call_count == 1
    with session_scope(vault) as s:
        assert s.scalar(select(func.count()).select_from(FnoBhav)) == 8
        assert s.get(FnoBhavDay, date(2008, 10, 16)).status == "failed"
        assert s.scalar(select(func.count()).select_from(FnoIvEod)) >= 1  # derived with the app's own code
        assert s.scalar(select(func.count()).select_from(FnoContract)) == 3
    assert "2008" in vb.status(vault) and vb.status(vault)["2008"] == {"failed": 2, "loaded": 1, "no_file": 1}


async def test_no_download_in_market_hours(vault):
    from finresearch.vault import backfill as vb

    with respx.mock(assert_all_called=False) as m:
        route = m.get(fb.url_for(DAY, "legacy"))
        r = await vb.backfill(DAY, DAY, vault_url=vault, http=client(),
                              **kw(now=lambda: datetime(2026, 10, 22, 11, 0, tzinfo=IST)))  # fmt: skip
    assert "market hours" in r["stopped"] and route.call_count == 0 and r["loaded"] == 0


async def test_two_refusals_in_a_row_stop_the_run_and_max_days_caps_it(vault):
    from finresearch.vault import backfill as vb

    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r".*/DERIVATIVES/.*").mock(return_value=httpx.Response(403))
        r = await vb.backfill(DAY, date(2008, 10, 24), vault_url=vault, http=client(), **kw())
        assert "403/429" in r["stopped"] and len(r["failed"]) == 2 and m.calls.call_count == 2
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r".*/DERIVATIVES/.*").mock(return_value=httpx.Response(404))
        r = await vb.backfill(date(2008, 11, 3), date(2008, 11, 14), vault_url=vault, http=client(),
                              **kw(max_days=3))  # fmt: skip
        assert (r["planned"], r["no_file"], r["remaining_after"], m.calls.call_count) == (3, 3, 7, 3)


async def test_refuses_without_a_data_rights_decision(vault, monkeypatch):
    from finresearch import data_rights
    from finresearch.vault import backfill as vb

    monkeypatch.delitem(data_rights.DECISIONS, "fno_bhav")
    with pytest.raises(data_rights.NoDecision):
        await vb.backfill(DAY, DAY, vault_url=vault, **kw())

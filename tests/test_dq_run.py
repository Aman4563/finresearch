"""The nightly data-quality run (#368): freshness per dataset against its SLA and the holiday calendar, the stored
dq_check / dataset_status rows, re-runs replacing rows, and the gate the pages read (unknown is never verified).
Synthetic data; test database only."""

from __future__ import annotations

from datetime import UTC, datetime

import dq_synth as ds
import pytest
from sqlalchemy import func, select, text

TABLES = ("fno_bhav, fno_bhav_day, fno_participant_oi, fno_iv_eod, index_eod, index_eod_day, span_param, span_day, "
          "fo_volt, option_quote, intraday_series, ipo_history, ipo_issue, groww_close_check, dataset_load, dq_check, "
          "dataset_status")  # fmt: skip
NIGHT = datetime(2026, 9, 30, 15, 30, tzinfo=UTC)  # Wed 21:00 IST: every SLA of the day has passed
FREE = lambda _p: 50 * 2**30  # noqa: E731


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))


def seed_fno(s, upto=ds.T1):
    from sqlalchemy import insert

    from finresearch.db.models import FnoIvEod, FnoParticipantOi, IndexEod, IndexEodDay

    ds.manifest(s, upto)
    ds.insert_bhav(s, ds.bhav_rows(ds.T0) + (ds.bhav_rows(ds.T1) if upto == ds.T1 else []))
    s.execute(insert(FnoParticipantOi), ds.participants(upto))
    s.execute(insert(IndexEod), ds.index_rows(upto))
    s.add(IndexEodDay(day=upto, status="loaded", url="synthetic", rows=3))
    s.add(FnoIvEod(day=upto, symbol="NIFTY", iv_30d=13, rate=0.065, iv_30d_method="interpolated"))


def status_of(s) -> dict[str, tuple]:
    from finresearch.db.models import DatasetStatus

    return {r.dataset: (r.status, r.lag_sessions, r.failed) for r in s.scalars(select(DatasetStatus))}


def test_fresh_clean_night_is_ok_and_rerun_replaces_rows(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import DqCheck
    from finresearch.dq import run

    with session_scope() as s:
        seed_fno(s)
    for _ in range(2):
        with session_scope() as s:
            out = run.run_all(s, NIGHT, holidays=set(), free_bytes=FREE)
    with session_scope() as s:
        st = status_of(s)
        n = s.scalar(select(func.count()).select_from(DqCheck))
    assert n == out["checks"]  # the second run replaced the first's rows
    for name in ("fno_bhav", "fno_participant_oi", "index_eod", "disk"):
        assert st[name][0] == "ok", (name, st[name])
    assert st["fno_bhav"][1] == 0
    assert st["span"][0] == "unknown"  # never loaded: unknown, not ok


def test_a_missed_day_fails_freshness_and_marks_the_gate_unverified(clean):
    from finresearch.db import session_scope
    from finresearch.dq import gate, run

    with session_scope() as s:
        seed_fno(s, upto=ds.T0)  # Wednesday's files never came
        run.run_all(s, NIGHT, holidays=set(), free_bytes=FREE)
    with session_scope() as s:
        st = status_of(s)
        assert st["fno_bhav"][:2] == ("fail", 1) and "freshness" in st["fno_bhav"][2]
        g = gate.health(s, ("fno_bhav", "fno_iv_eod"))
    assert g["status"] == "unverified" and "fno_bhav" in g["failed"]


def test_before_the_sla_yesterday_is_expected(clean):
    from finresearch.db import session_scope
    from finresearch.dq import run

    evening = datetime(2026, 9, 30, 14, 30, tzinfo=UTC)  # Wed 20:00 IST: before the 20:30 bhavcopy SLA
    with session_scope() as s:
        seed_fno(s, upto=ds.T0)
        run.run_all(s, evening, holidays=set(), free_bytes=FREE)
        assert status_of(s)["fno_bhav"][1] == 0


def test_unknown_holiday_list_leaves_freshness_unknown(clean, monkeypatch):
    from finresearch.adapters import nse_holidays
    from finresearch.db import session_scope
    from finresearch.dq import gate, run

    monkeypatch.setattr(nse_holidays, "known_trading_holidays", lambda _d: None)
    with session_scope() as s:
        seed_fno(s)
        run.run_all(s, NIGHT, holidays=None, free_bytes=FREE)
        assert status_of(s)["fno_bhav"][0] == "unknown"
        assert gate.health(s, ("fno_bhav",))["status"] == "unknown"


def test_gate_without_any_run_is_unknown(clean):
    from finresearch.db import session_scope
    from finresearch.dq import gate

    with session_scope() as s:
        g = gate.health(s, ("fno_bhav",))
    assert g["status"] == "unknown" and g["datasets"]["fno_bhav"]["status"] == "unknown"


def test_low_disk_fails(clean):
    from finresearch.db import session_scope
    from finresearch.dq import run

    with session_scope() as s:
        run.run_all(s, NIGHT, holidays=set(), free_bytes=lambda _p: 2**30)
        assert status_of(s)["disk"][0] == "fail"


def test_one_crashing_check_leaves_the_others_and_marks_its_dataset_unknown(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.dq import run

    def boom(s, day):
        raise RuntimeError("synthetic SQL failure")

    monkeypatch.setattr(run, "DATASETS", tuple(
        run.Dataset(d.name, d.sla, d.last, boom if d.name == "index_eod" else d.checks, d.critical) for d in run.DATASETS))  # fmt: skip
    with session_scope() as s:
        seed_fno(s)
        run.run_all(s, NIGHT, holidays=set(), free_bytes=FREE)
        st = status_of(s)
        note = s.execute(
            text("SELECT note FROM dq_check WHERE dataset = 'index_eod' AND \"check\" = 'checks'")
        ).scalar()
    assert st["fno_bhav"][0] == "ok" and st["index_eod"][0] == "unknown"
    assert "RuntimeError: synthetic SQL failure" in note


def test_disk_check_on_a_missing_path_is_unknown(tmp_path):
    from finresearch.dq import misc

    assert misc.disk_check(tmp_path / "nope").status == "unknown"

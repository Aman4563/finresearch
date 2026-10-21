"""The historical replay (#368 acceptance 2, run by the owner on the real history): every loaded bhavcopy day in a
range through the content checks, read only, with counts per check and the flagged days. Synthetic days here."""

from __future__ import annotations

import dq_synth as ds
import pytest
from sqlalchemy import func, insert, select, text

TABLES = (
    "fno_bhav, fno_bhav_day, fno_participant_oi, fno_iv_eod, index_eod, index_eod_day, dataset_load, dq_check"
)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))


def test_replay_flags_the_faulty_day_and_writes_nothing(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import DqCheck, FnoIvEod, FnoParticipantOi
    from finresearch.dq import replay

    with session_scope() as s:
        ds.manifest(s, ds.T1)
        ds.insert_bhav(s, ds.bhav_rows(ds.T0))
        ds.insert_bhav(
            s, [r for r in ds.bhav_rows(ds.T1) if r["symbol"] != "XYZSTK"]
        )  # a dropped symbol on T1
        for d in (ds.T0, ds.T1):
            s.execute(insert(FnoParticipantOi), ds.participants(d))
            s.add(FnoIvEod(day=d, symbol="NIFTY", iv_30d=13, rate=0.065, iv_30d_method="interpolated"))
    with session_scope() as s:
        res = replay.replay(s, ds.T0, ds.T1)
    assert res["sessions"] == 2
    assert res["flagged_days"]["fno_bhav:futures_continuity"] == [ds.T1.isoformat()]
    assert res["by_check"]["fno_bhav:futures_continuity"]["fail"] == 1
    assert ds.T1.isoformat() in res["critical_days"] and ds.T0.isoformat() not in res["critical_days"]
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(DqCheck)) == 0

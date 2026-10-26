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


def test_replay_reports_warning_rates_for_the_screens_and_charts(clean):
    """The owner's measurement for the non-critical screens and charts (#368): a warn rate per check and the share of
    sessions with any warning. The E1 24,800 call is priced 1.05 on T1 only (about 275 under its intrinsic value), so
    opt_bounds warns on 1 of the 2 sessions; the other new checks are present in by_check."""
    from decimal import Decimal

    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod
    from finresearch.dq import replay

    rows = ds.bhav_rows(ds.T1)
    bad = next(r for r in rows if r["instrument"] == "IDO" and r["expiry"] == ds.E1 and r["strike"] == 24800
               and r["option_type"] == "CE")  # fmt: skip
    bad["close"], bad["settle"] = Decimal("1.00"), Decimal("1.05")
    with session_scope() as s:
        ds.manifest(s, ds.T1)
        ds.insert_bhav(s, ds.bhav_rows(ds.T0) + rows)
        for d in (ds.T0, ds.T1):
            s.add(FnoIvEod(day=d, symbol="NIFTY", iv_30d=13, rate=0.065, iv_30d_method="interpolated"))
    with session_scope() as s:
        res = replay.replay(s, ds.T0, ds.T1)
    assert res["by_check"]["fno_bhav:opt_bounds"] == {"ok": 1, "warn": 1}
    assert (
        res["warn_rate"]["fno_bhav:opt_bounds"] == 0.5
        and res["warn_rate"]["fno_bhav:futures_continuity"] == 0
    )
    assert {"fno_bhav:opt_butterfly", "fno_bhav:opt_calendar", "fno_bhav:cc_rows_total", "fno_bhav:cc_settle_dispersion",
            "fno_iv_eod:cc_iv30", "fno_iv_eod:cc_iv30_vix_gap"} <= set(res["by_check"])  # fmt: skip
    assert res["warning_days"] == [ds.T1.isoformat()] and res["warning_rate"] == 0.5
    assert not any(
        k.startswith("broker_groww") for k in res["by_check"]
    )  # personal, and no history to replay

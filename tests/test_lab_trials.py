"""#380 (P2.5): every Strategy Lab run is a logged trial (lab_trial, append-only), in-sample runs included, with its
daily P&L vector, and the trial count N of the deflated Sharpe counts every distinct spec ever run plus every vault
read that counts in N (vault PREREG.md §2.5). N can only grow."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_stratlab import lab_db, mem, run_now, spec  # noqa: F401  (fixtures)


def _trials():
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import LabTrial

    with session_scope() as s:
        return [
            {
                "run_id": t.run_id,
                "spec_hash": t.spec_hash,
                "mode": t.mode,
                "status": t.status,
                "daily": t.daily,
            }
            for t in s.scalars(select(LabTrial).order_by(LabTrial.id))
        ]


def test_lab_trial_refuses_update_and_delete(lab_db):  # noqa: F811
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("INSERT INTO lab_trial (spec_hash, underlying, mode, status, daily) "
                       "VALUES ('h', 'NIFTY', 'is', 'failed', '[]')"))  # fmt: skip
    for sql in ("UPDATE lab_trial SET status = 'done'", "DELETE FROM lab_trial"):
        with pytest.raises(DBAPIError, match="lab_trial is append-only"), session_scope() as s:
            s.execute(text(sql))


def test_an_in_sample_run_is_a_logged_trial_with_its_daily_pnl_and_raises_n(lab_db, mem):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.db.models import LabRun
    from finresearch.stratlab import store

    with session_scope() as s:
        assert store.n_trials(s) == 8  # the library's VARIANTS_TRIED, nothing from the lab yet
        st = store.create_strategy(s, "Bull put", spec(), mem)
        v1, is_end, cap = st.versions[0].id, st.is_end, spec().capital
    got = run_now(v1, False, mem)
    assert got["n_trials"] == 9  # an in-sample run is a trial too (#380)
    [t] = _trials()
    assert (t["run_id"], t["mode"], t["status"]) == (got["id"], "is", "done")
    days = [d for d, _ in t["daily"]]
    assert days == sorted(days) and days[-1] <= is_end.isoformat()
    with session_scope() as s:
        r = s.get(LabRun, got["id"])
        assert "daily" not in r.result  # the vector lives in the trial log only
    # the daily P&L adds up to the change in marked equity over the window (the first day against the capital)
    last_equity = got["result"]["equity"][-1][1]
    assert sum(x for _, x in t["daily"]) == pytest.approx(last_equity - cap, abs=0.01)

    run_now(v1, False, mem)  # the same spec again: a second logged trial, not a new variant
    with session_scope() as s:
        assert len(_trials()) == 2 and store.n_trials(s) == 9
        v2 = store.add_version(s, _strategy(s), spec(delta=0.15)).id
    assert run_now(v2, False, mem)["n_trials"] == 10


def _strategy(s):
    from sqlalchemy import select

    from finresearch.db.models import LabStrategy

    return s.scalars(select(LabStrategy)).one()


def test_a_failed_run_is_logged_and_still_counts(lab_db, mem):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.stratlab import jobs, store

    class Broken:
        def latest(self, sym):
            raise RuntimeError("history store unreachable")

    with session_scope() as s:
        v1 = store.create_strategy(s, "", spec(), mem).versions[0].id
        rid = jobs.submit(s, v1, False).id
    assert jobs.execute(rid, Broken()) == "failed"
    [t] = _trials()
    assert (t["run_id"], t["status"], t["daily"]) == (rid, "failed", [])
    with session_scope() as s:
        assert store.n_trials(s) == 9


def test_a_run_marked_stale_is_logged(lab_db, mem):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.db.models import LabRun
    from finresearch.stratlab import jobs, store

    with session_scope() as s:
        v1 = store.create_strategy(s, "", spec(), mem).versions[0].id
        r = jobs.submit(s, v1, True)
        rid, created = r.id, r.created_at
    with session_scope() as s:
        assert jobs.recover_stale(s, created + jobs.START_GRACE + timedelta(seconds=1)) == 1
        assert s.get(LabRun, rid).status == "failed"
    [t] = _trials()
    assert (t["run_id"], t["mode"], t["status"]) == (rid, "oos", "failed")


def test_vault_reads_that_count_in_n_raise_the_lab_n(lab_db):  # noqa: F811
    from sqlalchemy import text

    from finresearch.db import session_scope
    from finresearch.db.models import VaultRead
    from finresearch.stratlab import store

    with session_scope() as s:
        s.add(VaultRead(study_id="vault_data_audit", study_hash="a" * 64, read_no=1, pack_hash="p" * 64,
                        engine_hash="e" * 64, counts_in_n=False, purpose="audit"))  # fmt: skip
        s.add(VaultRead(study_id="vrp_primary", study_hash="b" * 64, read_no=1, pack_hash="p" * 64,
                        engine_hash="e" * 64, counts_in_n=True, purpose="study"))  # fmt: skip
    with session_scope() as s:
        assert store.n_trials(s) == 9 and store.vault_reads_in_n(s) == 1
    with session_scope() as s:
        s.execute(text("TRUNCATE vault_read"))  # keep it clean for later tests


def test_the_trial_log_is_in_the_forward_only_offsite_backup():
    """Its rows cannot be fetched again (a re-run is a new trial), like card_log (#380)."""
    from finresearch.backups import offsite

    assert "lab_trial" in offsite.FORWARD_ONLY

"""#361 (P0.11) interim Strategy Lab relabel: the Lab's "out-of-sample" window is already published, so a run that
passes every criterion reads "in-sample result", never "candidate" (also for runs stored before the change), and every
result and split carries the label. The library's own statuses (backtest/) are untouched."""

from __future__ import annotations

import pytest
from test_stratlab import lab_db, mem, run_now, save_library, spec  # noqa: F401  (fixtures)


@pytest.fixture
def passing(monkeypatch):
    """Every library criterion passes (the judge is not under test here)."""
    from finresearch.stratlab import evaluate as ev

    monkeypatch.setattr(ev.lib, "judge", lambda *a, **k: [{"criterion": "all", "pass": True, "rule": "x"}])


def test_a_passing_lab_run_reads_in_sample_result_not_candidate(lab_db, mem, passing):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.stratlab import split, store

    save_library([0.01, -0.02, 0.03, 0.0, -0.01, 0.02, -0.03, 0.015])
    with session_scope() as s:
        v1 = store.create_strategy(s, "Bull put", spec(), mem).versions[0].id
    got = run_now(v1, True, mem)["result"]
    assert got["status"] == "in_sample_result" and got["status"] != "candidate"
    assert got["oos_label"] == split.OOS_LABEL == "already published, not out of sample"
    assert got["reasons"] == [
        "passes every library criterion on an already-published window (in-sample result)"
    ]


def test_runs_stored_as_candidate_before_the_relabel_read_in_sample_result(lab_db, mem):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.db.models import LabRun, LabVersion
    from finresearch.stratlab import split, store

    with session_scope() as s:
        v1 = store.create_strategy(s, "Bull put", spec(), mem).versions[0].id
    rid = run_now(v1, True, mem)["id"]
    with session_scope() as s:
        r = s.get(LabRun, rid)
        r.result = {**r.result, "status": "candidate"}  # as stored by the old code
        s.flush()
        full, brief = store.run_json(r, full=True), store.run_json(r)
        st = store.strategy_json(s, s.get(LabVersion, r.version_id).strategy)
    assert full["result"]["status"] == "in_sample_result" and brief["summary"]["status"] == "in_sample_result"
    assert full["result"]["oos_label"] == split.OOS_LABEL and brief["summary"]["oos_label"] == split.OOS_LABEL
    assert st["split"]["oos_label"] == split.OOS_LABEL
    assert all(x["summary"]["status"] != "candidate" for v in st["versions"] for x in v["runs"])


def test_the_library_status_is_unchanged():
    """WP-A keys real-money sizing on the library's own `candidate` (backtest/evaluate): not relabelled here."""
    import inspect

    from finresearch.backtest import evaluate as lib

    assert '"candidate"' in inspect.getsource(lib)

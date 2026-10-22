"""#380 (P2.5): the Lab's sealed holdout = forward sessions after the spec's registration date (the first time its hash
was saved, in any strategy). The Lab never reads the vault (evals/experiments/vault/PREREG.md §2.4). The published
out-of-sample window keeps its #361 label; only the holdout can read "candidate"."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from test_stratlab import lab_db, mem, run_now, spec  # noqa: F401  (fixtures)

REG = datetime(2025, 3, 31, 12, 0, tzinfo=UTC)  # registered 31-Mar-2025 (IST): the holdout starts 1-Apr-2025


def test_the_holdout_is_the_sessions_after_registration(lab_db, mem):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.stratlab import store

    with session_scope() as s:
        v1 = store.create_strategy(s, "Bull put", spec(), mem, now=REG).versions[0].id
        assert store.registered_on(s, store.spec_hash(spec())) == date(2025, 3, 31)
    res = run_now(v1, True, mem)["result"]
    h = res["holdout"]
    assert (h["from"], h["to"]) == ("2025-04-01", "2025-08-29")
    w = res["windows"]["holdout"]
    n_forward = sum(1 for t in run_now(v1, True, mem)["trades"] if t["entry_day"] >= "2025-04-01")
    assert w["trades"] == n_forward and w["from"] == "2025-04-01"
    assert h["status"] in ("candidate", "fails") and h["criteria"]
    assert res["status"] in (
        "in_sample_result",
        "fails",
    )  # the published window is still labelled seen (#361)


def test_a_spec_registered_after_the_last_stored_day_has_no_holdout_yet(lab_db, mem):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.stratlab import store

    with session_scope() as s:
        v1 = store.create_strategy(s, "", spec(), mem, now=datetime(2026, 10, 1, tzinfo=UTC)).versions[0].id
    h = run_now(v1, True, mem)["result"]["holdout"]
    assert h["status"] == "too_early" and h["from"] == "2026-10-02" and "criteria" not in h


def test_the_registration_is_the_first_save_of_the_hash_in_any_strategy(lab_db, mem):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.stratlab import store

    with session_scope() as s:
        store.create_strategy(s, "first", spec(), mem, now=REG)
        later = store.create_strategy(s, "again", spec(), mem, now=datetime(2025, 7, 1, tzinfo=UTC))
        assert store.registered_on(s, later.versions[0].spec_hash) == date(2025, 3, 31)


def test_only_the_holdout_can_read_candidate(lab_db, mem, monkeypatch):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.stratlab import evaluate as ev
    from finresearch.stratlab import store

    monkeypatch.setattr(ev.lib, "judge", lambda *a, **k: [{"criterion": "all", "pass": True, "rule": "x"}])
    with session_scope() as s:
        v1 = store.create_strategy(s, "", spec(), mem, now=REG).versions[0].id
    res = run_now(v1, True, mem)["result"]
    assert res["status"] == "in_sample_result" and res["holdout"]["status"] == "candidate"


def test_lab_versions_are_append_only(lab_db, mem):  # noqa: F811
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from finresearch.db import session_scope
    from finresearch.stratlab import store

    with session_scope() as s:
        store.create_strategy(s, "", spec(), mem, now=REG)
    for sql in ("UPDATE lab_version SET created_at = now()", "DELETE FROM lab_version"):
        with pytest.raises(DBAPIError, match="lab_version is append-only"), session_scope() as s:
            s.execute(text(sql))

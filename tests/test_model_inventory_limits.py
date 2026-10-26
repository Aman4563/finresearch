"""The Kupiec trigger's mark in the model inventory (#378, skill-monitor-v2): GET /api/models and `finresearch models
list` show "not for limits" for a VaR model whose logged exceptions failed Kupiec in 3 consecutive quarters, read only
from the forecast ledger. Synthetic VaR-ledger rows on the test database only."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from skillmon_synth import Q1, Q2, Q3, quarter, store
from sqlalchemy import text
from starlette.testclient import TestClient

from finresearch.fincalc import dates

AFTER_Q3 = datetime(2026, 10, 26, 11, tzinfo=dates.IST)


@pytest.fixture
def api(env):
    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(
            text("TRUNCATE forecast, fno_backtest_result, fno_backtest_run, model_revalidation CASCADE")
        )
    with TestClient(create_app(clock=lambda: AFTER_Q3)) as c:
        yield c
    with session_scope() as s:
        s.execute(text("TRUNCATE forecast"))


def _entries(api) -> dict:
    return {e["key"]: e for e in api.get("/api/models").json()["entries"]}


def test_the_var_models_carry_the_trigger_and_no_other_entry_does(api) -> None:
    by = _entries(api)  # no logged VaR yet
    assert by["risk_var_ledger"]["limits"]["status"] == "insufficient data"
    assert by["risk_var"]["limits"]["status"] == "insufficient data"
    assert by["direction_logistic"]["limits"] is None and by["risk_margin_approx"]["limits"] is None

    store(quarter(Q1, 60, 0) + quarter(Q2, 60, 8, first_id=100) + quarter(Q3, 60, 0, first_id=200))
    by = _entries(api)
    for key in ("risk_var_ledger", "risk_var"):
        lim = by[key]["limits"]
        assert lim["status"] == "not for limits" and lim["run"] == ["2026-Q1", "2026-Q2", "2026-Q3"]
        assert [q["status"] for q in lim["quarters"]] == ["fail"] * 3 and lim["version"] == "skill-monitor-v2"
    from finresearch.db import session_scope

    with session_scope() as s:  # the GETs wrote nothing: the ledger still holds exactly the stored rows
        assert s.execute(text("SELECT count(*) FROM forecast")).scalar() == 180


def test_a_changed_v2_registration_reads_unknown_never_fine(api, monkeypatch) -> None:
    from finresearch.skillmon import prereg

    store(quarter(Q1, 60, 3) + quarter(Q2, 60, 3, first_id=100) + quarter(Q3, 60, 3, first_id=200))
    monkeypatch.setattr(prereg, "V2_HASH", "0" * 64)
    lim = _entries(api)["risk_var"]["limits"]
    assert lim["status"] == "unknown" and "refused" in lim["why"]


def test_models_list_prints_the_mark(api, monkeypatch) -> None:
    from typer.testing import CliRunner

    from finresearch.cli import app

    store(quarter(Q1, 60, 0) + quarter(Q2, 60, 0, first_id=100) + quarter(Q3, 60, 0, first_id=200))
    monkeypatch.setattr(dates, "today_ist", lambda: date(2026, 10, 26))
    r = CliRunner().invoke(app, ["models", "list"], env={"COLUMNS": "250"})  # one entry per line
    assert r.exit_code == 0, r.output
    lines = {ln.split()[0]: ln for ln in r.output.splitlines() if ln.strip()}
    assert "not for limits" in lines["risk_var_ledger"] and "not for limits" in lines["risk_var"]
    assert "limits" not in lines["direction_logistic"]

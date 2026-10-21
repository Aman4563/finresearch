"""#379 through the API: the model inventory (read-only) and the saved backtest's evidence status."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from finresearch.fincalc import dates

CLOCK = datetime(2026, 10, 22, 11, tzinfo=dates.IST)


@pytest.fixture
def api(env):
    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE fno_backtest_result, fno_backtest_run CASCADE"))
    with TestClient(create_app(clock=lambda: CLOCK)) as c:
        yield c


def test_models_lists_every_entry_with_a_computed_status_and_the_limits_of_use(api):
    from finresearch.inventory import registry

    body = api.get("/api/models").json()
    assert body["as_of"] == "2026-10-22" and body["max_evidence_age_days"] == 45
    assert "no automated orders" in body["limits_of_use"] and set(body["tiers"]) == {"A", "B", "C"}
    by = {e["key"]: e for e in body["entries"]}
    assert set(by) == {e.key for e in registry.entries()}
    gate = by["fno_strategy_gate"]
    assert gate["tier"] == "A" and gate["status"] == "unvalidated" and gate["outcome_metric"]
    assert by["direction_logistic"]["status"] == "failed"
    assert ("2025-09-01", "expiry_weekday") in {(c["day"], c["tag"]) for c in body["regime_changes"]}


def test_the_backtest_route_says_when_the_saved_run_is_stale(api):
    from finresearch.backtest import store
    from finresearch.db import session_scope

    rep = {"version": "t", "registration_hash": "h", "registered_hash": "h", "oos_evaluated": True, "source": "t",
           "variants_tried": 8, "stock_rule_symbol": None, "strategies": []}  # fmt: skip
    with session_scope() as s:
        store.save(s, rep)
        s.execute(text("UPDATE fno_backtest_run SET created_at = '2025-08-01T12:00:00+05:30'"))
    st = api.get("/api/fno/backtests").json()["run"]["evidence_status"]
    assert st["current"] is False and any("days old" in w for w in st["stale"])
    assert any("01-Sep-2025" in w for w in st["stale"]) and "provenance unknown" in st["code_changed"]

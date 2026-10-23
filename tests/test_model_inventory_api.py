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
        s.execute(text("TRUNCATE fno_backtest_result, fno_backtest_run, model_revalidation CASCADE"))
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


def test_models_carry_evidence_age_reviews_and_the_latest_re_validation(api):
    from finresearch.backtest import store
    from finresearch.db import session_scope
    from finresearch.db.models import ModelRevalidation

    rep = {"version": "t", "registration_hash": "h", "registered_hash": "h", "oos_evaluated": True, "source": "t",
           "variants_tried": 8, "stock_rule_symbol": None, "strategies": []}  # fmt: skip
    with session_scope() as s:
        s.execute(text("TRUNCATE model_revalidation"))
        store.save(s, rep)
        s.execute(text("UPDATE fno_backtest_run SET created_at = '2026-10-02T12:00:00+05:30'"))
        s.add(ModelRevalidation(slot="revalidate:2026-10", model_key="recipe:nifty_ic_w", split="from_2025-09-01",
                                status="computed", metrics={"expectancy": -12.5, "trades": 40}, code_hash="c",
                                engine_hash="e", registration_hash="r", regime_tags={}, source="t"))  # fmt: skip
    body = api.get("/api/models").json()
    by = {e["key"]: e for e in body["entries"]}
    ic = by["recipe:nifty_ic_w"]
    assert ic["evidence_as_of"] == "2026-10-02" and ic["evidence_age_days"] == 20  # 22-Oct − 2-Oct
    assert ic["revalidation"]["splits"]["from_2025-09-01"]["metrics"]["expectancy"] == -12.5
    assert ic["status"] == "failed"  # a re-validation never changes a status: post hoc, no candidate
    gate = by["fno_strategy_gate"]
    assert gate["evidence_age_days"] is None and gate["revalidation"] is None  # unknown, never 0
    assert gate["red_team"] == [] and body["redteam_invalid"] == []
    assert (
        body["revalidation"]["after"] == "22:30" and body["revalidation"]["splits"][0] == "before_2024-11-20"
    )


def test_models_without_a_saved_backtest_say_the_recipes_evidence_age_is_unknown(api):
    body = api.get("/api/models").json()
    ic = next(e for e in body["entries"] if e["key"] == "recipe:nifty_ic_w")
    assert ic["evidence_as_of"] is None and ic["evidence_age_days"] is None and ic["revalidation"] is None

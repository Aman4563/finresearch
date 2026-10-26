"""skill-monitor-v2 in the nightly pass and the morning brief (#378): input drift and the VaR models' Kupiec mark are
stored under the report's "v2" key; an alarm (an input at act, a VaR model not for limits) is a red line counted in the
headline, and inputs or models that could not be checked are said so, never shown as ok. Test database only;
synthetic panels (tests/direction_synth.py) and made-up VaR-ledger rows."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import direction_synth as ds
import pytest
from skillmon_synth import Q1, Q2, Q3, quarter, store
from sqlalchemy import text

from finresearch.monitor import skill as job
from finresearch.skillmon import drift

NIGHT = datetime(2026, 10, 26, 16, 0, tzinfo=UTC)  # 21:30 IST
BRIEF_AT = NIGHT + timedelta(hours=11)  # 08:30 IST next day


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe() -> None:
        with session_scope() as s:
            s.execute(text("TRUNCATE forecast"))
            s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'skill:%'"))

    wipe()
    yield env
    wipe()


def _panels(monkeypatch) -> None:
    """NIFTY: 2022-24 at IV 15, then IV 25 (a planted shift); BANKNIFTY: no stored session at all."""
    nifty = ds.panel(date(2022, 1, 3), date(2024, 6, 28), seed=3) + ds.panel(
        date(2024, 7, 1), date(2025, 10, 31), seed=4, iv=25.0
    )
    monkeypatch.setattr(drift, "_load", lambda sym: nifty if sym == "NIFTY" else [])


def test_the_nightly_pass_stores_drift_and_the_risk_marks(clean, monkeypatch) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot

    _panels(monkeypatch)
    store(quarter(Q1, 60, 0) + quarter(Q2, 60, 0, first_id=100) + quarter(Q3, 60, 0, first_id=200))
    res = job.skill_step(NIGHT)
    assert (
        res["skill"]["v2_alarms"] == 4
    )  # card and direction at act on NIFTY, both VaR models not for limits
    with session_scope() as s:
        r = s.get(AlertEvalSlot, "skill:2026-10-26").result
    v2 = r["v2"]
    assert r["status"] == "done" and v2["version"] == "skill-monitor-v2"
    by = {(g["model"], g["underlying"]): g for g in v2["drift"]["groups"]}
    assert (
        by[("card", "NIFTY")]["status"] == "act"
        and by[("card", "BANKNIFTY")]["status"] == "insufficient data"
    )
    assert v2["risk"]["marks"]["risk_var"]["status"] == "not for limits"


def test_a_refused_v2_keeps_the_cell_checks(clean, monkeypatch) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot
    from finresearch.skillmon import prereg

    monkeypatch.setattr(prereg, "V2_HASH", "0" * 64)
    job.skill_step(NIGHT)
    with session_scope() as s:
        r = s.get(AlertEvalSlot, "skill:2026-10-26").result
        sec = job.brief_section(s, BRIEF_AT)
    assert r["status"] == "done" and r["version"] == "skill-monitor-v1" and r["v2"]["status"] == "refused"
    assert any("skill-monitor-v2" in x["text"] and "unchecked" in x["text"] for x in sec["lines"])


def _stored(day: str, v2: dict | None) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot

    rep = {"status": "done", "attempts": 1, "generated_at": NIGHT.isoformat(), "alarms": 0, "cells": []}
    with session_scope() as s:
        s.add(AlertEvalSlot(slot=f"skill:{day}", result=rep if v2 is None else {**rep, "v2": v2}))


V2 = {"version": "skill-monitor-v2",
      "drift": {"alarms": 1, "groups": [
          {"model": "card", "underlying": "NIFTY", "label": "F&O card · NIFTY", "status": "act",
           "act": [{"input": "iv_30", "psi": 1.234}, {"input": "rv_20", "psi": 0.5}], "watch": [], "insufficient": [],
           "reference": ["2022-04-01", "2024-06-30"], "live_to": "2026-10-26"},
          {"model": "direction", "underlying": "NIFTY", "label": "F&O direction logistic · NIFTY", "status": "watch",
           "act": [], "watch": [{"input": "iv_rank", "psi": 0.12}], "insufficient": ["fii_fut_net", "pro_fut_net"],
           "reference": ["2022-01-01", "2024-06-30"], "live_to": "2026-10-26"},
          {"model": "card", "underlying": "BANKNIFTY", "label": "F&O card · BANKNIFTY", "status": "unknown",
           "act": [], "watch": [], "insufficient": [], "why": "BANKNIFTY: OperationalError: no database"}]},
      "risk": {"marks": {k: {"status": "not for limits", "run": ["2026-Q1", "2026-Q2", "2026-Q3"],
                             "why": "Kupiec p < 0.05 in 3 consecutive quarters (2026-Q1 to 2026-Q3)"}
                         for k in ("risk_var_ledger", "risk_var")}}}  # fmt: skip


def test_the_brief_shows_v2_alarms_in_red_and_says_what_is_unchecked(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.monitor.digest import brief_text, build_brief

    _stored("2026-10-26", V2)
    with session_scope() as s:
        b = build_brief(s, BRIEF_AT)
    warn = [h["text"] for h in b["health"] if h["level"] == "warn"]
    info = [h["text"] for h in b["health"] if h["level"] == "info"]
    assert len(b["skill"]["alarms"]) == 3 and "3 forecast skill alarms" in b["headline"]
    assert any("F&O card · NIFTY" in t and "iv_30 1.23, rv_20 0.50" in t for t in warn)
    assert sum("not for limits" in t for t in warn) == 2
    assert any("on watch" in t and "iv_rank 0.12" in t for t in info)
    assert any("2 input(s)" in t and "not ok" in t for t in info)  # too few values: unchecked, never ok
    assert any("F&O card · BANKNIFTY" in t and "unknown" in t for t in info)
    assert "• Skill alarm: F&O card · NIFTY" in brief_text(b)


def test_a_report_from_before_v2_says_drift_and_the_trigger_are_unchecked(clean) -> None:
    from finresearch.db import session_scope

    _stored("2026-10-26", None)
    with session_scope() as s:
        sec = job.brief_section(s, BRIEF_AT)
    assert sec["alarms"] == [] and any(
        "not run yet" in x["text"] and "drift" in x["text"] for x in sec["lines"]
    )


def test_cli_skill_prints_drift_and_the_marks_read_only(clean, monkeypatch) -> None:
    from typer.testing import CliRunner

    from finresearch.cli import app
    from finresearch.db import session_scope

    _panels(monkeypatch)
    r = CliRunner().invoke(app, ["forecasts", "skill"], env={"COLUMNS": "250"})
    assert r.exit_code == 0, r.output
    assert (
        "F&O card · NIFTY" in r.output
        and "act" in r.output
        and "risk_var_ledger: insufficient data" in r.output
    )
    with session_scope() as s:  # read only: no slot claimed
        assert s.execute(text("SELECT count(*) FROM alert_eval_slot WHERE slot LIKE 'skill:%'")).scalar() == 0


def test_each_unchecked_var_model_gets_its_own_line_never_ok(clean) -> None:
    from finresearch.db import session_scope

    marks = {k: {"status": "insufficient data", "run": None, "why": "0 of the 3 tested quarters needed"}
             for k in ("risk_var_ledger", "risk_var")}  # fmt: skip
    _stored(
        "2026-10-26",
        {"version": "skill-monitor-v2", "drift": {"groups": [], "alarms": 0}, "risk": {"marks": marks}},
    )
    with session_scope() as s:
        sec = job.brief_section(s, BRIEF_AT)
    lines = [x["text"] for x in sec["lines"] if "Kupiec trigger reads insufficient data" in x["text"]]
    assert sec["alarms"] == [] and len(lines) == 2  # specs_v2.BRIEF: an info line per model
    assert {t.split(":")[0] for t in lines} == {
        "Portfolio 1-day 95 % VaR (forecast ledger)",
        "Portfolio historical VaR / CVaR",
    }

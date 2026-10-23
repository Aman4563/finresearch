"""#379 second slice: the monthly re-validation re-runs the backtest-backed models as a read-only replay and appends
per-regime-split results to model_revalidation. It never writes fno_backtest_run (the evidence the cards read), and it
can create no candidate (a post hoc analysis, issue #379)."""

from __future__ import annotations

from datetime import date, datetime

import fno_synth as fs
import pytest
from sqlalchemy import func, select, text

from finresearch.fincalc import dates
from finresearch.inventory import revalidate as rv

NOW = datetime(2026, 11, 1, 22, tzinfo=dates.IST)


def _t(entry: str, net: float, expiry: str) -> dict:
    return {
        "entry_day": entry,
        "net": net,
        "expiry": expiry,
        "days_held": 5,
        "margin": None,
        "entry_costs": 1.0,
        "exit_costs": 1.0,
        "exercise_stt": 0.0,
    }


def test_splits_are_the_issue_s_regime_boundaries():
    got = rv.splits()
    assert [(s.key, s.lo, s.hi) for s in got] == [
        ("before_2024-11-20", None, date(2024, 11, 19)),
        ("2024-11-20_to_2025-08-31", date(2024, 11, 20), date(2025, 8, 31)),
        ("from_2025-09-01", date(2025, 9, 1), None),
    ]


def test_split_rows_put_each_trade_in_the_split_of_its_entry_day():
    rep = {"strategies": [{"key": "nifty_ic_w", "name": "IC", "status": "fails", "trades": [
        _t("2024-11-19", 100.0, "2024-11-21"), _t("2024-11-20", -40.0, "2024-11-28"),
        _t("2025-08-31", 20.0, "2025-09-04"), _t("2025-09-01", 7.0, "2025-09-02")]}]}  # fmt: skip
    rows = {r["split"]: r for r in rv.split_rows(rep)}
    assert rows["before_2024-11-20"]["metrics"]["trades"] == 1
    assert rows["before_2024-11-20"]["metrics"]["expectancy"] == 100.0
    mid = rows["2024-11-20_to_2025-08-31"]["metrics"]
    assert mid["trades"] == 2 and mid["expectancy"] == -10.0  # (-40 + 20) / 2
    assert rows["from_2025-09-01"]["metrics"]["expectancy"] == 7.0
    assert {r["model_key"] for r in rows.values()} == {"recipe:nifty_ic_w"}
    assert all(r["status"] == "computed" for r in rows.values())


def test_a_split_without_trades_is_no_data_never_zero():
    rep = {"strategies": [{"key": "nifty_ic_w", "name": "IC", "status": "fails",
                           "trades": [_t("2025-09-03", 5.0, "2025-09-09")]}]}  # fmt: skip
    rows = {r["split"]: r for r in rv.split_rows(rep)}
    assert rows["before_2024-11-20"]["status"] == "no_data" and rows["before_2024-11-20"]["metrics"] is None
    assert "no trade" in rows["before_2024-11-20"]["reason"]


def test_a_strategy_with_no_history_is_no_data_with_its_reason():
    rep = {
        "strategies": [{"key": "stock_cc_m", "status": "no_data", "reasons": ["no stock options are stored"]}]
    }
    rows = rv.split_rows(rep)
    assert len(rows) == 3 and all(
        r["status"] == "no_data" and "no stock options" in r["reason"] for r in rows
    )


def test_split_rows_carry_no_status_that_could_read_as_a_candidate():
    rep = {
        "strategies": [
            {"key": "nifty_ic_w", "status": "candidate", "trades": [_t("2025-09-03", 5, "2025-09-09")]}
        ]
    }
    for r in rv.split_rows(rep):
        assert "candidate" not in str(r) and r["status"] in ("computed", "no_data")


# --------------------------------------------------------------------------------------------- with the database
@pytest.fixture
def db(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE model_revalidation, fno_backtest_result, fno_backtest_run CASCADE"))
    return session_scope


def test_a_replay_appends_rows_and_never_touches_the_saved_backtest(db, monkeypatch):
    from finresearch.backtest import store as bstore
    from finresearch.db.models import FnoBacktestRun, ModelRevalidation

    def refuse(*a, **k):
        raise AssertionError("re-validation must never save a backtest run")

    monkeypatch.setattr(bstore, "save", refuse)
    mem = fs.history(date(2024, 9, 2), date(2025, 1, 31))
    out = rv.run(mem, slot="revalidate:2026-11", source="synthetic", now=NOW)
    assert out["rows"] > 0 and out["slot"] == "revalidate:2026-11"
    with db() as s:
        assert s.scalar(select(func.count()).select_from(FnoBacktestRun)) == 0
        rows = s.scalars(select(ModelRevalidation)).all()
        assert len(rows) == out["rows"] and {r.split for r in rows} == {s.key for s in rv.splits()}
        r = rows[0]
        assert len(r.code_hash) == 64 and r.regime_tags and r.registration_hash and r.data_hash is None
    again = rv.run(
        mem, slot="revalidate:2026-11", source="synthetic", now=NOW
    )  # a retried slot appends nothing
    assert again["rows"] == 0
    with db() as s:
        assert s.scalar(select(func.count()).select_from(ModelRevalidation)) == out["rows"]


def test_latest_by_model_reads_the_newest_slot_per_split(db):
    from finresearch.db.models import ModelRevalidation

    with db() as s:
        for slot, exp in (("revalidate:2026-10", 1.0), ("revalidate:2026-11", 2.0)):
            s.add(ModelRevalidation(slot=slot, model_key="recipe:x", split="from_2025-09-01", status="computed",
                                    metrics={"expectancy": exp}, code_hash="c", engine_hash="e",
                                    registration_hash="r", regime_tags={}, source="t"))  # fmt: skip
    with db() as s:
        got = rv.latest_by_model(s)
    assert got["recipe:x"]["slot"] == "revalidate:2026-11"
    assert got["recipe:x"]["splits"]["from_2025-09-01"]["metrics"]["expectancy"] == 2.0


def test_the_cli_re_validates_into_the_table_only(db, monkeypatch):
    from typer.testing import CliRunner

    from finresearch.backtest import dbhistory
    from finresearch.cli import app
    from finresearch.db.models import FnoBacktestRun, ModelRevalidation

    mem = fs.history(date(2024, 9, 2), date(2025, 1, 31))
    monkeypatch.setattr(dbhistory, "DbHistory", lambda url=None: mem)
    monkeypatch.setattr(dbhistory, "top_stock", lambda h, a, b: None)
    r = CliRunner().invoke(app, ["models", "revalidate", "--slot", "revalidate:2026-11"])
    assert r.exit_code == 0, r.output
    with db() as s:
        assert s.scalar(select(func.count()).select_from(FnoBacktestRun)) == 0
        rows = s.scalars(select(ModelRevalidation)).all()
        assert rows and {r.slot for r in rows} == {"revalidate:2026-11"}
        assert all(len(r.data_hash or "") == 64 for r in rows)  # the manifest of the database the replay read

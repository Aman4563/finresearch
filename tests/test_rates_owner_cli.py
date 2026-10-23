"""#479: `finresearch rates enter|owner|remove|show|policy` (test database; nothing is fetched)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from finresearch.cli import app


@pytest.fixture
def db(env, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.fincalc import dates
    from finresearch.signals import rates

    monkeypatch.setattr(rates, "OWNER", None)
    monkeypatch.setattr(dates, "today_ist", lambda: date(2026, 10, 23))
    with session_scope() as s:
        s.execute(text("TRUNCATE owner_rate"))
    yield env


def run(*args: str):
    return CliRunner().invoke(app, ["rates", *args])


def test_enter_list_show_and_remove(db):
    res = run(
        "enter", "--day", "2026-09-30", "--tenor", "91D", "--yield", "5.5199", "--source", "RBI auction"
    )
    assert res.exit_code == 0, res.output
    listed = run("owner")
    assert listed.exit_code == 0 and "2026-09-30" in listed.output and "91D" in listed.output
    shown = json.loads(run("show", "--day", "2026-10-23", "--tenor", "91D").output)
    # 5.5199 % at repo 5.25 %, shifted by the 07-Oct-2026 hike of 25 bp
    assert shown["tier"] == "owner" and shown["shift_bp"] == 25 and shown["yield"] == pytest.approx(0.057699)
    assert run("remove", "--day", "2026-09-30", "--tenor", "91D").exit_code == 0
    assert run("remove", "--day", "2026-09-30", "--tenor", "91D").exit_code == 1  # nothing left to remove
    assert json.loads(run("show", "--day", "2026-10-23").output)["tier"] == "proxy"


def test_enter_refuses_a_bad_entry(db):
    res = run("enter", "--day", "2026-10-24", "--tenor", "91D", "--yield", "5.7")
    assert res.exit_code == 2 and "future" in res.output


def test_policy_prints_the_table_with_its_sources(db):
    res = run("policy")
    assert res.exit_code == 0 and "07-Oct-2026" in res.output and "5.50" in res.output

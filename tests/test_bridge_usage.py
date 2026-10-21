"""`finresearch bridge usage`: Max-window spend per agent role across recent runs (#338)."""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from finresearch.cli import app, usage_by_role


def _step(run_id, key, role, cost, before=None, after=None, tin=0, tout=0, turns=0, secs=0.0):
    return SimpleNamespace(run_id=run_id, key=key, role=role, cost_usd_est=cost, five_hour_before=before,
                           five_hour_after=after, input_tokens=tin, output_tokens=tout, num_turns=turns,
                           duration_s=secs)  # fmt: skip


def test_groups_by_role_with_cost_shares():
    rows = usage_by_role([
        _step(1, "stream:financials", "financials", 3.0, 0.20, 0.32, tin=1000, tout=10, turns=40, secs=600),
        _step(2, "stream:financials", "financials", 1.0, 0.10, 0.12, tin=500, tout=5, turns=20, secs=300),
        _step(1, "verify:risks", "verifier", 1.0, 0.25, 0.27, tin=100, tout=1, turns=5, secs=60),
    ])  # fmt: skip
    fin, ver = rows
    assert fin["role"] == "financials" and fin["steps"] == 2 and fin["runs"] == 2
    assert fin["cost"] == 4.0 and fin["share"] == 0.8 and ver["share"] == 0.2
    assert (fin["in_tok"], fin["out_tok"], fin["turns"], fin["minutes"]) == (1500, 15, 60, 15.0)
    assert abs(fin["mean_5h_delta"] - 0.07) < 1e-9 and fin["with_5h"] == 2


def test_unknown_values_stay_unknown():
    rows = usage_by_role([
        _step(1, "planner", "planner", None, 0.1, None),
        _step(1, "critic", None, None),
    ])  # fmt: skip
    by = {r["role"]: r for r in rows}
    assert (
        by["planner"]["no_cost"] == 1
        and by["planner"]["mean_5h_delta"] is None
        and by["planner"]["with_5h"] == 0
    )
    assert by["critic"]["share"] is None  # no known cost anywhere: no share, not 0%
    assert by["critic"]["role"] == "critic"  # falls back to the step key's prefix


def test_cli_reads_recent_research_runs(env):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, ResearchRun

    with session_scope() as s:
        runs = [
            ResearchRun(kind=k, status="done", manifest={}) for k in ("ipo_report", "ipo_report", "smoke_mcp")
        ]
        s.add_all(runs)
        s.flush()
        s.add(AgentStep(run_id=runs[0].id, key="stream:demand", stage="stream", role="demand", status="done",
                        cost_usd_est=2.5, input_tokens=1234567, num_turns=30, five_hour_before=0.1,
                        five_hour_after=0.15))  # fmt: skip
        s.add(AgentStep(run_id=runs[1].id, key="planner", stage="plan", role="planner", status="done",
                        cost_usd_est=0.5))  # fmt: skip
        s.add(AgentStep(run_id=runs[2].id, key="smoke", stage="plan", role="smoke-only", status="done",
                        cost_usd_est=9.0))  # fmt: skip
    res = CliRunner().invoke(app, ["bridge", "usage", "--runs", "5"], env={"COLUMNS": "200"})
    assert res.exit_code == 0, res.output
    assert "demand" in res.output and "83%" in res.output and "1,234,567" in res.output
    assert "smoke-only" not in res.output  # not a research run
    only = CliRunner().invoke(app, ["bridge", "usage", "--runs", "1"], env={"COLUMNS": "200"})
    assert "planner" in only.output and "demand" not in only.output  # newest run only

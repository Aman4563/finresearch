"""Stock research report verdicts follow the informational policy (#241): a research view (favourable / mixed /
unfavourable), never BUY…AVOID, and no entry zone presented as an instruction; existing reports render the same way."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "eval" / "infosys-run9.json"


@pytest.mark.parametrize(("old", "new"), [("BUY", "FAVOURABLE"), ("ACCUMULATE", "FAVOURABLE"), ("HOLD", "MIXED"),
                                          ("REDUCE", "UNFAVOURABLE"), ("AVOID", "UNFAVOURABLE"), ("mixed", "MIXED")])  # fmt: skip
def test_stored_synthesis_with_an_old_verdict_still_loads_as_a_view(old, new):
    from finresearch.agents.schemas import StockSynthesis

    s = StockSynthesis(verdict=old, horizon="3-5 years", confidence="medium", executive_summary="-", reasons_for=[],
                       reasons_against=[], scenarios=[], action_checklist=[], report_markdown="-")  # fmt: skip
    assert s.verdict == new


def test_schema_offers_only_view_words_to_the_synthesizer():
    from finresearch.agents.schemas import StockSynthesis, json_schema_for

    enum = json_schema_for(StockSynthesis)["properties"]["verdict"]["enum"]
    assert enum == ["FAVOURABLE", "MIXED", "UNFAVOURABLE"]


def test_policy_follows_calls_enabled(monkeypatch):
    from finresearch.signals import stock

    assert stock.CALLS_ENABLED is False  # the PIT experiment has not passed its bar
    assert (
        stock.report_view("REDUCE")["label"]
        == "Research view: unfavourable — informational, no validated edge"
    )
    md = "## Verdict box\n| Item | Value |\n|---|---|\n| Verdict | **BUY** |\n"
    monkeypatch.setattr(stock, "CALLS_ENABLED", True)  # once a model earns calls, reports say what they said
    assert stock.report_view("BUY") is None and stock.neutral_report_markdown(md) == md


def test_existing_infosys_report_renders_as_a_research_view():
    from finresearch.signals.stock import neutral_report_markdown

    md = json.loads(FIXTURE.read_text())["synthesis"]["report_markdown"]
    box = md.split("\n## ", 2)[1]  # the original verdict box says ACCUMULATE and has an entry zone
    assert "**ACCUMULATE**" in box and "Entry zone" in box
    out = neutral_report_markdown(md)
    new_box = out.split("\n## ", 2)[1]
    assert "**ACCUMULATE**" not in new_box and "**Favourable** (research view, informational" in new_box
    assert "Entry zone" not in new_box and "Price range discussed (context, not an instruction)" in new_box
    assert out.split("\n## ", 2)[2] == md.split("\n## ", 2)[2]  # the rest of the report is as written


def test_report_route_serves_the_neutral_verdict_box(env, tmp_path):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.evals.replay import import_run

    with session_scope() as s:
        run_id = import_run(s, json.loads(FIXTURE.read_text()), slug_suffix="-" + tmp_path.name[-8:])
        assert s.get(ResearchRun, run_id).kind == "stock_report"
    with TestClient(create_app()) as c:
        md = c.get(f"/api/runs/{run_id}/report").json()["markdown"]
    assert "**Favourable**" in md and "**ACCUMULATE**" not in md.split("\n## ", 2)[1]


def test_stock_report_views_are_scored_and_shown_informational():
    from types import SimpleNamespace as NS

    from finresearch.signals.ledger import _call_of, verdict_probability

    # the fixed confidence map v1: medium = 0.65 that the direction is right
    assert verdict_probability("stock_report", "FAVOURABLE", "medium") == (0.65, "up")
    assert verdict_probability("stock_report", "UNFAVOURABLE", "medium") == (0.35, "down")
    assert verdict_probability("stock_report", "MIXED", "high") == (None, "no call")
    row = NS(asset="stock", source="run:9", action="ACCUMULATE", score=None, inputs={})
    call = _call_of(row)
    assert call["status"] == "informational" and call["tilt"] == "research view: favourable"
    assert _call_of(NS(asset="ipo", source="run:5", action="APPLY", score=None, inputs={})) is None


def test_since_report_levels_read_as_context_not_instructions():
    from finresearch.api.insights import _level_label

    assert _level_label("entry_zone") == "Price range discussed"
    assert _level_label("entry_zone_pe") == "Price range discussed"
    assert _level_label("max_buy_price") == "Price ceiling discussed"
    assert _level_label("fair_value_pe") != "Price range discussed"  # other estimates keep their own label

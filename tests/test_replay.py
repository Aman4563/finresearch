"""Offline replay of a real run: the gold evaluation, the publish gate and the back-test run in CI without Claude."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.evals.backtest import classify

FIXTURE = Path(__file__).parent / "fixtures" / "eval" / "orient-cables-run5.json"


@pytest.fixture
def replayed(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep
    from finresearch.evals.replay import import_run

    data = json.loads(FIXTURE.read_text())
    with session_scope() as s:
        run_id = import_run(s, data, slug_suffix="-" + tmp_path.name[-8:])
        report = (s.query(AgentStep).filter_by(run_id=run_id, stage="synthesis", status="done")
                  .order_by(AgentStep.finished_at.desc()).first().output)  # fmt: skip
    return data, run_id, report["report_markdown"]


def test_live_run_5_still_passes_the_release_bar(replayed):
    """Guards the scorer, the gold set and the remap: live Orient Cables run 5 scored 22/22 with 0 contradicted."""
    from finresearch.db import session_scope
    from finresearch.evals.gold import evaluate, load_gold

    data, run_id, report = replayed
    with session_scope() as s:
        res = evaluate(s, run_id, load_gold("orient-cables"), report)
    assert res.recall == 1.0 and res.verified_recall == 1.0 and not res.contradicted
    assert res.passes_release_bar and res.verdict_agrees
    assert res.stats["claims_total"] == len(data["claims"]) == 434 and res.stats["steps"] == 44


def test_the_replayed_report_passes_the_publish_gate_with_remapped_citations(replayed):
    import re

    from finresearch.db import session_scope
    from finresearch.verify.gate import check_report

    data, run_id, report = replayed
    original = {int(x) for x in re.findall(r"\[C(\d+)\]", data["synthesis"]["report_markdown"])}
    remapped = {int(x) for x in re.findall(r"\[C(\d+)\]", report)}
    assert len(original) == len(remapped) > 100
    with session_scope() as s:
        from finresearch.db.models import Claim

        in_run = {c.id for c in s.query(Claim).filter_by(run_id=run_id)}
        assert remapped <= in_run  # every citation resolves to a claim of the replayed run
        gate = check_report(s, run_id, report)
    assert gate.ok, gate.blocking[:3]


def test_export_of_a_replayed_run_round_trips(replayed):
    from finresearch.db import session_scope
    from finresearch.evals.replay import export_run

    data, run_id, _ = replayed
    with session_scope() as s:
        again = export_run(s, run_id)
    strip = lambda c: {k: v for k, v in c.items() if k not in ("id", "corrects_claim_id", "citations")}  # noqa: E731
    assert [strip(c) for c in again["claims"]] == [strip(c) for c in data["claims"]]
    assert (
        again["synthesis"]["overall_verdict"] == data["synthesis"]["overall_verdict"] == "APPLY-CONDITIONAL"
    )


def test_backtest_scores_verdicts_against_listing_outcomes():
    assert classify("APPLY", Decimal("10.3")) == "hit" and classify("APPLY", Decimal("-2")) == "miss"
    assert classify("AVOID", Decimal("-5")) == "hit" and classify("AVOID", Decimal("12")) == "miss"
    assert classify("APPLY-CONDITIONAL", Decimal("10")) == "conditional"
    assert classify("APPLY (listing gains only)", None) == "pending"


def test_backtest_reads_the_monitor_listing_price(replayed):
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun, Watch
    from finresearch.evals.backtest import backtest, markdown

    _, run_id, _ = replayed
    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        from datetime import date

        s.add(Watch(company_id=run.company_id, nse_symbol="ORIENTCABL", open_date=date(2026, 9, 25),
                    close_date=date(2026, 9, 29), allotment_date=date(2026, 9, 30), listing_date=date(2026, 10, 5),
                    meta={"listing_open": "300"}))  # fmt: skip
        s.flush()
        row = next(r for r in backtest(s) if r.run_id == run_id)
        assert (
            row.listing_gain_pct.quantize(Decimal("0.01")) == Decimal("10.29") and row.result == "conditional"
        )
        assert "APPLY-CONDITIONAL | +10.29%" in markdown([row])


def test_live_infosys_stock_run_9_passes_the_release_bar(env, tmp_path):
    """The first listed-stock kind acceptance run: 21/23 key facts, 0 contradicted, publish gate passed."""
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep
    from finresearch.evals.gold import evaluate, load_gold
    from finresearch.evals.replay import import_run
    from finresearch.verify.gate import check_report

    data = json.loads((FIXTURE.parent / "infosys-run9.json").read_text())
    with session_scope() as s:
        run_id = import_run(s, data, slug_suffix="-" + tmp_path.name[-8:])
        report = (s.query(AgentStep).filter_by(run_id=run_id, stage="synthesis", status="done")
                  .order_by(AgentStep.finished_at.desc()).first().output["report_markdown"])  # fmt: skip
        res = evaluate(s, run_id, load_gold("infosys"), report)
        gate = check_report(s, run_id, report)
    assert res.passes_release_bar and res.recall >= 0.9 and res.high_recall == 1.0 and gate.ok
    assert data["run"]["kind"] == "stock_report" and data["synthesis"]["verdict"] == "ACCUMULATE"
    assert res.verdict_actual["overall"] == "ACCUMULATE"


def test_live_fund_run_11_passes_the_release_bar(env, tmp_path):
    """The mutual-fund kind acceptance run (Axis Midcap Fund Direct Growth): 8/8 key facts, 0 contradicted."""
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep
    from finresearch.evals.gold import evaluate, load_gold
    from finresearch.evals.replay import import_run
    from finresearch.verify.gate import check_report

    data = json.loads((FIXTURE.parent / "mf-120505-run11.json").read_text())
    with session_scope() as s:
        run_id = import_run(s, data, slug_suffix="-" + tmp_path.name[-8:])
        report = (s.query(AgentStep).filter_by(run_id=run_id, stage="synthesis", status="done")
                  .order_by(AgentStep.finished_at.desc()).first().output["report_markdown"])  # fmt: skip
        res = evaluate(s, run_id, load_gold("mf-120505"), report)
        gate = check_report(s, run_id, report)
    assert res.passes_release_bar and res.recall == 1.0 and gate.ok and data["synthesis"]["verdict"] == "HOLD"


def test_live_bond_run_12_passes_the_release_bar(env, tmp_path):
    """The bond kind acceptance run (L&T Finance 8.98% NCD 2029): 3/3 listing facts, 0 contradicted, gate passed."""
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep
    from finresearch.evals.gold import evaluate, load_gold
    from finresearch.evals.replay import import_run
    from finresearch.verify.gate import check_report

    data = json.loads((FIXTURE.parent / "bond-ine027e07998-run12.json").read_text())
    with session_scope() as s:
        run_id = import_run(s, data, slug_suffix="-" + tmp_path.name[-8:])
        report = (s.query(AgentStep).filter_by(run_id=run_id, stage="synthesis", status="done")
                  .order_by(AgentStep.finished_at.desc()).first().output["report_markdown"])  # fmt: skip
        res = evaluate(s, run_id, load_gold("bond-ine027e07998"), report)
        gate = check_report(s, run_id, report)
    assert res.passes_release_bar and gate.ok and data["synthesis"]["verdict"] == "AVOID"


@pytest.fixture
def ist_local_time(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Asia/Kolkata")  # east of UTC, where the overflow happens
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_export_of_an_unfinished_run(env, tmp_path, ist_local_time):
    """Regression: steps without finished_at were sorted with datetime.min.astimezone(), which overflows."""
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, ResearchRun
    from finresearch.evals.replay import export_run
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "unfinished-" + tmp_path.name[-8:], "U")
        run = ResearchRun(company_id=co.id, kind="ipo_report", status="paused", manifest={})
        s.add(run)
        s.flush()
        s.add(AgentStep(run_id=run.id, key="synthesis", stage="synthesis", role="synthesizer", status="done",
                        output={"report_markdown": "# r"}))  # fmt: skip
        s.add(AgentStep(run_id=run.id, key="critic:r1", stage="critic", role="critic", status="deferred"))
        s.flush()
        out = export_run(s, run.id)
    assert out["synthesis"]["step"] == "synthesis" and len(out["steps"]) == 2

"""`finresearch evidence regrade` (#285): grade-D fincalc citations stored before #265's argument binding are checked
again. Synthetic ledger rows in the pre-#265 shape (no bindings/constants/args_bound); no LLM, no network."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event

URL = "https://news.example.com/acme-results"


def _legacy_run(tmp_path, args, *, importance="high"):
    """A run with input claims 200 (FY25) and 230 (FY26), and a "Revenue grew 15%" claim whose only citation is a
    grade-D fincalc citation saved before #265, plus a finished report that cites it. Returns (run, claim, citation,
    input ids)."""
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, Citation, Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "rg-" + tmp_path.name[-8:], "Acme Cables Ltd")
        run = ResearchRun(company_id=co.id, kind="ipo_report", manifest={})
        s.add(run)
        s.flush()
        ins = []
        for v, period in (("200", "FY2025"), ("230", "FY2026")):
            c = Claim(run_id=run.id, stream="financials", statement=f"Revenue ₹{v} crore", claim_type="numeric",
                      metric="revenue", value=Decimal(v), unit="INR crore", period=period, status="verified",
                      citations=[Citation(url=URL, quote=f"Revenue {v}")])  # fmt: skip
            s.add(c)
            ins.append(c)
        s.flush()
        ids = [c.id for c in ins]
        # (new - old) / old: what save_claim stored for a fincalc citation from #242 until #265
        res = (Decimal(args["new"]) - Decimal(args["old"])) / Decimal(args["old"])
        legacy = Citation(url=f"fincalc:growth.pct_change({args})", quote="growth.pct_change = 0.15", quote_found=True,
                          accessed_at=datetime.now(UTC),
                          computation={"function": "growth.pct_change", "args": args, "result_key": None,
                                       "result": str(res), "inputs": ids, "matches": True, "inputs_ok": True,
                                       "detail": "fincalc reproduces the value; inputs cited"})  # fmt: skip
        growth = Claim(run_id=run.id, stream="financials", statement="Revenue grew 15%", claim_type="numeric",
                       metric="revenue_growth", value=Decimal("15"), unit="%", period="FY2026", status="verified",
                       importance=importance, citations=[legacy])  # fmt: skip
        s.add(growth)
        s.flush()
        s.add(AgentStep(run_id=run.id, key="synthesis", stage="synthesis", status="done",
                        finished_at=datetime.now(UTC),
                        output={"report_markdown": f"Revenue grew 15% in FY2026 [C{growth.id}]."}))  # fmt: skip
        return run.id, growth.id, legacy.id, ids


def _stored(citation_id):
    from finresearch.db import session_scope
    from finresearch.db.models import Citation
    from finresearch.verify.evidence import citation_grade

    with session_scope() as s:
        ct = s.get(Citation, citation_id)
        return citation_grade(ct)[0], ct.quote_found, ct.computation


def _regrade(**kw):
    from finresearch.db import session_scope
    from finresearch.verify.regrade import regrade

    with session_scope() as s:
        res = regrade(s, who="tester", **kw)
        if not kw.get("apply"):
            s.rollback()
        return res


def _mine(res, citation_id):
    return next(r for r in res.citations if r.citation_id == citation_id)


def test_old_d_citation_with_matching_inputs_stays_d_and_gains_bindings(env, tmp_path):
    run_id, claim_id, cit, ids = _legacy_run(tmp_path, {"old": "200", "new": "230"})
    res = _regrade(apply=True)
    r = _mine(res, cit)
    assert (r.grade, r.changed, r.run_id, r.claim_id) == ("D", True, run_id, claim_id)
    grade, found, comp = _stored(cit)
    assert grade == "D" and found is True
    assert comp["bindings"] == {"new": ids[1], "old": ids[0]} and comp["args_bound"] is True
    assert comp["regrade"]["by"] == "tester" and "#285" in comp["regrade"]["why"]
    assert "args_bound" not in comp["regrade"]["before"]["computation"]  # the pre-#265 row, kept for a revert
    # the gate is unchanged: no report is listed as changing
    assert not any(g.changes for g in res.gates if g.run_id == run_id)


def test_old_d_citation_on_figures_nobody_cited_becomes_u_and_blocks_the_report(env, tmp_path):
    # (115 - 100) / 100 = 15 % reproduces the stated 15 %, but the cited claims are 200 and 230 (#265)
    run_id, claim_id, cit, _ = _legacy_run(tmp_path, {"old": "100", "new": "115"})
    res = _regrade(apply=True)
    r = _mine(res, cit)
    assert r.grade == "U" and r.changed and "new, old match no cited input claim" in r.reason
    grade, found, comp = _stored(cit)
    assert grade == "U" and found is False and comp["args_bound"] is False
    assert comp["regrade"]["before"]["quote_found"] is True
    g = next(g for g in res.gates if g.run_id == run_id)
    assert (g.before_ok, g.after_ok, g.changes) == (True, False, True)
    assert any(f"[C{claim_id}]" in b and "grade U" in b for b in g.new_blocking)

    from finresearch.db import session_scope
    from finresearch.db.models import Claim

    with session_scope() as s:  # the claim's status is left alone; the gate blocks on its grade
        assert s.get(Claim, claim_id).status == "verified"


@pytest.fixture
def writes(env):
    """Every INSERT/UPDATE/DELETE statement sent to any database while the test runs (listened on the Engine class:
    session_scope's engine is cached under another key than get_engine())."""
    from sqlalchemy.engine import Engine

    seen: list[str] = []
    engine = Engine

    def record(conn, cursor, statement, *a):
        if statement.lstrip().split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


def test_dry_run_reports_the_change_and_writes_nothing(env, tmp_path, writes):
    run_id, claim_id, cit, _ = _legacy_run(tmp_path, {"old": "100", "new": "115"})
    before = _stored(cit)
    writes.clear()
    res = _regrade()
    assert not res.applied and writes == []
    r = _mine(res, cit)
    assert r.grade == "U" and r.changed
    g = next(g for g in res.gates if g.run_id == run_id)
    assert (g.before_ok, g.after_ok) == (True, False) and any(f"[C{claim_id}]" in b for b in g.new_blocking)
    assert _stored(cit) == before == ("D", True, before[2]) and "args_bound" not in before[2]


def test_apply_is_idempotent(env, tmp_path, writes):
    _, _, good, _ = _legacy_run(tmp_path, {"old": "200", "new": "230"})
    _, _, bad, _ = _legacy_run(tmp_path / "b", {"old": "100", "new": "115"})
    first = _regrade(apply=True)
    assert {(_mine(first, c).grade, _mine(first, c).changed) for c in (good, bad)} == {
        ("D", True),
        ("U", True),
    }
    stored = (_stored(good), _stored(bad))
    writes.clear()
    second = _regrade(apply=True)  # the re-checked rows now carry args_bound: nothing left to do
    assert all(r.citation_id not in (good, bad) for r in second.citations) and writes == []
    every = _regrade(apply=True, all_d=True)  # every grade-D row again: the same result, so no new stamp
    assert _mine(every, good).changed is False and writes == []
    assert (_stored(good), _stored(bad)) == stored


def test_regrade_cli_dry_run_prints_the_table_and_the_gate_change(env, tmp_path, writes):
    from typer.testing import CliRunner

    from finresearch.cli import app

    run_id, claim_id, cit, _ = _legacy_run(tmp_path, {"old": "100", "new": "115"})
    writes.clear()
    out = CliRunner().invoke(app, ["evidence", "regrade"], env={"COLUMNS": "250"})
    assert out.exit_code == 0, out.output
    assert f"C{claim_id}" in out.output and "downgraded to U" in out.output and "dry run" in out.output
    assert f"run {run_id}: publish gate PASSED -> BLOCKED" in out.output
    assert writes == [] and _stored(cit)[0] == "D"
    out = CliRunner().invoke(app, ["evidence", "regrade", "--apply"], env={"COLUMNS": "250"})
    assert out.exit_code == 0, out.output
    assert "APPLIED" in out.output and writes and _stored(cit)[0] == "U"

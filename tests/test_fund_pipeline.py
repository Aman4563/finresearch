"""The mutual-fund kind: AMFI baseline facts and the whole pipeline through a fake runner."""

from __future__ import annotations

import json
import time
from decimal import Decimal

from finresearch.agents.roles import FUND_STREAMS
from finresearch.agents.schemas import (
    CaseReport,
    ClaimVerdict,
    CriticReport,
    FundSynthesis,
    ResearchPlan,
    StreamFocus,
    StreamReport,
    VerificationReport,
)
from finresearch.bridge.limits import LimitTracker
from finresearch.bridge.types import AgentResult, RateLimitSnapshot, Tier
from finresearch.orchestrator.fund import fund_facts

NAV_DATA = {
    "scheme": {"scheme_code": "120505", "name": "Axis Midcap Fund", "plan": "Direct Plan", "option": "Growth Option",
               "category": "Equity Scheme - Mid Cap Fund", "amc": "Axis Mutual Fund", "nav": "138.78",
               "nav_date": "2026-09-28", "isin": "INF846K01EH3"},
    "first": {"date": "2021-09-20", "nav": "76.51"}, "last": {"date": "2026-09-28", "nav": "138.78"},
    "stats": {"points": 1240, "trailing_1y": "0.0717499776", "trailing_3y": "0.1549533269",
              "trailing_5y": "0.1276420770", "annualised_volatility": "0.1460696711538416965518665544",
              "max_drawdown": "-0.2034457478005865102639296188",
              "rolling_3y": {"window_years": "3", "count": "104", "minimum": "0.1306191493", "median": "0.18986839885",
                             "maximum": "0.2589472163", "share_positive": "1"}},
}  # fmt: skip


def test_fund_baseline_facts_are_amfi_values():
    facts = {m: (v, u, per) for m, v, u, per, *_ in fund_facts(NAV_DATA)}
    assert facts["nav"] == ("138.78", "INR per unit", "2026-09-28")
    assert facts["return_3y_annualised"][:2] == ("15.4953", "%") and facts["max_drawdown"][0] == "-20.3446"
    assert (
        facts["rolling_3y_return_minimum"][0] == "13.0619"
        and facts["rolling_3y_return_maximum"][0] == "25.8947"
    )


class FundRunner:
    def __init__(self, run_id):
        self.run_id, self.calls = run_id, []

    async def __call__(self, role, ctx, **extra):
        from finresearch.db import session_scope
        from finresearch.db.models import Claim

        self.calls.append(role)
        if role == "fund_planner":
            assert ctx.nse_symbol == "120505" and "mutual-fund" in ctx.subject
            out = ResearchPlan(company_one_liner="x", critical_questions=["q"],
                               streams=[StreamFocus(stream=s, questions=["?"]) for s in FUND_STREAMS])  # fmt: skip
        elif role in FUND_STREAMS:
            with session_scope() as s:
                c = Claim(run_id=self.run_id, stream=role, statement=f"{role} fact", claim_type="factual")
                s.add(c)
                s.flush()
                cid = c.id
            out = StreamReport(
                summary=role, section_markdown=f"{role} [C{cid}]", key_findings=[], claim_ids=[cid]
            )
        elif role == "verifier":
            ids = [c["claim_id"] for c in json.loads(extra["claims"])]
            out = VerificationReport(verdicts=[ClaimVerdict(claim_id=i, verdict="verified", evidence="ok") for i in ids],
                                     summary="ok")  # fmt: skip
        elif role in ("fund_bull", "fund_bear"):
            out = CaseReport(
                thesis=role, points=[], listing_view="-", long_term_view="-", strongest_counterargument="-"
            )
        elif role == "fund_synthesizer":
            out = FundSynthesis(verdict="SIP ONLY", suits="5y+ horizon", confidence="medium", executive_summary="-",
                                reasons_for=[], reasons_against=[], action_checklist=[],
                                report_markdown=f"# Axis Midcap\\n{extra['stream_reports'][:40]}")  # fmt: skip
        elif role == "fund_critic":
            out = CriticReport(gaps=[], ready_to_publish=True)
        else:
            raise AssertionError(role)
        return out, AgentResult(task_name=role, tier=Tier.CLAUDE_MAX, model="fake", ok=True, structured_output={},
                                rate_limit=RateLimitSnapshot(five_hour_utilization=0.1,
                                                             five_hour_resets_at=int(time.time()) + 3600))  # fmt: skip


async def test_fund_pipeline_runs_fund_roles_with_amfi_facts(env, tmp_path, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.mcp_server import server
    from finresearch.orchestrator.base import PipelineConfig, create_run
    from finresearch.orchestrator.kinds import pipeline_for
    from finresearch.render.pack import render_pack

    slug = "mf-120505-" + tmp_path.name[-6:]
    with session_scope() as s:
        co = get_or_create_company(s, slug, "Axis Midcap Fund (Direct Plan, Growth Option)")
        co.meta = {"amfi_code": "120505"}
    run_id = create_run(slug, kind="fund_report")

    async def fake_history(code, years=5, risk_free_annual=None):
        assert code == "120505"
        return json.dumps(NAV_DATA)

    monkeypatch.setattr(server, "amfi_nav_history", fake_history)
    runner = FundRunner(run_id)
    pipe = pipeline_for(run_id, runner=runner, tracker=LimitTracker(tmp_path / "limits"),
                        config=PipelineConfig(concurrency=3, render=False))  # fmt: skip
    assert await pipe.run() == "done"
    assert set(FUND_STREAMS) <= set(runner.calls) and {"fund_synthesizer", "fund_critic"} <= set(runner.calls)
    with session_scope() as s:
        facts = {c.metric: c.value for c in s.query(Claim).filter_by(run_id=run_id, stream="facts")}
        m = s.get(ResearchRun, run_id).manifest
    assert facts["nav"] == Decimal("138.78") and facts["return_5y_annualised"] == Decimal("12.7642")
    assert (
        m["kind"] == "fund_report"
        and m["facts"]["fund"]["scheme"]["category"] == "Equity Scheme - Mid Cap Fund"
    )
    pack = render_pack(run_id, pdf=False)
    assert (pack.path / "02_Performance_and_Risk" / "fund_performance_section.md").exists()
    assert "mutual-fund research report" in (pack.path / "06_Final_Report" / "report.html").read_text()


def test_fund_search_and_add_through_the_api(env):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from finresearch.adapters.amfi import parse_nav_all
    from finresearch.api import create_app

    rows = parse_nav_all((Path(__file__).parent / "fixtures" / "amfi" / "NAVAll_trimmed.txt").read_text())

    async def nav_all():
        return rows

    with TestClient(create_app(nav_all=nav_all)) as c:
        hits = c.get("/api/funds/search", params={"q": "axis midcap"}).json()
        assert hits[0]["scheme_code"] == "120505" and hits[0]["category"] == "Equity Scheme - Mid Cap Fund"
        made = c.post("/api/funds", json={"scheme_code": "120505"}).json()
        assert made["slug"] == "mf-120505" and made["name"] == "Axis Midcap Fund (Direct Plan, Growth Option)"
        assert c.get("/api/funds/search", params={"q": "120505"}).json()[0]["slug"] == "mf-120505"
        assert c.post("/api/funds", json={"scheme_code": "999999"}).status_code == 404

"""The listed-stock kind: NSE baseline facts, stock discovery, and the whole pipeline through a fake runner."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.adapters.nse import Quote
from finresearch.adapters.nse_equity import Announcement, AnnualReportFiling, CorporateAction, Shareholding
from finresearch.agents.roles import STOCK_STREAMS
from finresearch.agents.schemas import (
    CaseReport,
    ClaimVerdict,
    CriticReport,
    ResearchPlan,
    StockSynthesis,
    StreamFocus,
    StreamReport,
    VerificationReport,
)
from finresearch.bridge.limits import LimitTracker
from finresearch.bridge.types import AgentResult, RateLimitSnapshot, Tier
from finresearch.fincalc.market import dividend_per_share
from finresearch.verify.stock_baseline import stock_facts

FIX = Path(__file__).parent / "fixtures" / "nse"


def market():
    quote = Quote.parse(json.loads((FIX / "quote_INFY_20260928.json").read_text()))
    sh = [Shareholding.parse(r) for r in json.loads((FIX / "equity" / "shareholding_INFY.json").read_text())]
    acts = [CorporateAction.parse(r) for r in json.loads((FIX / "equity" / "actions_INFY.json").read_text())]
    return quote, sh, acts


def test_stock_baseline_facts_from_recorded_nse_payloads():
    quote, sh, acts = market()
    facts = {f.metric: f for f in stock_facts("INFY", quote, sh, acts, date(2026, 9, 29))}
    # recorded at 16:00 (after the close): the official close, which equals the last trade here
    assert "last_price" not in facts and facts["close_price"].value == Decimal("1003.2")
    assert facts["close_price"].quote == "closePrice 1003.2" and facts["week52_low"].value == Decimal("982.4")
    # market cap = issued shares x official close, equal to NSE's own totalMarketCap in the same payload
    assert facts["market_cap"].value == Decimal("4071218805878.4") and facts[
        "promoter_holding"
    ].value == Decimal("13.82")
    ttm = facts["dividend_per_share_ttm"]
    assert ttm.value == 48 and "Interim Dividend - Rs 23" in ttm.quote and "Rs 22" not in ttm.quote  # 25 + 23
    assert facts["close_price"].period == "2026-09-28 16:00 IST"


def test_dividend_subjects_with_several_dividends_are_summed():
    assert (
        dividend_per_share(
            "Annual General Meeting/Special Dividend - Rs 8 Per Share /Dividend - Rs 20 Per Share"
        )
        == 28
    )


async def test_stock_discovery_takes_latest_annual_reports_and_results_filings():
    from finresearch.ingest.discover import stock_candidates
    from finresearch.ingest.documents import DocKind

    ar = json.loads((FIX / "equity" / "annual_reports_INFY.json").read_text())["data"]
    anns = json.loads((FIX / "equity" / "announcements_INFY_trimmed.json").read_text())

    class FakeEquity:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def annual_reports(self, symbol):
            return [AnnualReportFiling.parse(r, symbol) for r in ar]

        async def announcements(self, symbol):
            return [Announcement.parse(r) for r in anns]

    cands = await stock_candidates("INFY", equity=FakeEquity(), results=2)
    assert [(c.kind, c.title) for c in cands] == [
        (DocKind.ANNUAL_REPORT, "Annual report FY26"), (DocKind.ANNUAL_REPORT, "Annual report FY25"),
        (DocKind.FINANCIALS, "Financial results for the period ended 2026-06-30"),
        (DocKind.FINANCIALS, "Financial results for the period ended 2026-03-31")]  # fmt: skip
    assert "_2025_2026_U_" in cands[0].url  # the revised FY26 filing replaces the original


class StockRunner:
    def __init__(self, run_id):
        self.run_id, self.calls = run_id, []

    async def __call__(self, role, ctx, **extra):
        from finresearch.db import session_scope
        from finresearch.db.models import Claim

        self.calls.append(role)
        if role == "stock_planner":
            assert "listed Indian stock" in ctx.subject
            out = ResearchPlan(company_one_liner="x", critical_questions=["q"],
                               streams=[StreamFocus(stream=s, questions=["?"]) for s in STOCK_STREAMS])  # fmt: skip
        elif role in STOCK_STREAMS:
            with session_scope() as s:
                c = Claim(run_id=self.run_id, stream=role, statement=f"{role} fact", claim_type="numeric",
                          metric="revenue_from_operations", value=Decimal(100 + len(role)), unit="INR crore",
                          period=f"FY{20 + len(role) % 7}")  # fmt: skip
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
        elif role in ("stock_bull", "stock_bear"):
            out = CaseReport(
                thesis=role, points=[], listing_view="-", long_term_view="-", strongest_counterargument="-"
            )
        elif role == "stock_synthesizer":
            out = StockSynthesis(verdict="HOLD", horizon="3-5 years", confidence="medium", executive_summary="-",
                                 reasons_for=[], reasons_against=[], scenarios=[], action_checklist=[],
                                 report_markdown=f"# Infosys\\n{extra['stream_reports'][:40]}")  # fmt: skip
        elif role == "stock_critic":
            out = CriticReport(gaps=[], ready_to_publish=True)
        else:
            raise AssertionError(role)
        return out, AgentResult(task_name=role, tier=Tier.CLAUDE_MAX, model="fake", ok=True, structured_output={},
                                rate_limit=RateLimitSnapshot(five_hour_utilization=0.1,
                                                             five_hour_resets_at=int(time.time()) + 3600))  # fmt: skip


@pytest.fixture
def stock_run(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Document
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.orchestrator.base import create_run

    slug = "infy-" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:8]
    txt = tmp_path / "ar.txt"
    txt.write_text("Annual report text")
    with session_scope() as s:
        co = get_or_create_company(s, slug, "Infosys Limited")
        co.nse_symbol = "INFY"
        s.add(Document(company_id=co.id, kind="ANNUAL_REPORT", title="Annual report FY26", sha256=hashlib.sha256(
            slug.encode()).hexdigest(), local_path=str(txt), text_path=str(txt), bytes=1, pages=1))  # fmt: skip
    return create_run(slug, kind="stock_report"), slug


async def test_stock_pipeline_runs_the_stock_roles_records_facts_and_renders_the_stock_pack(
    stock_run, tmp_path, monkeypatch
):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.orchestrator import stock as stock_mod
    from finresearch.orchestrator.base import PipelineConfig
    from finresearch.orchestrator.kinds import pipeline_for
    from finresearch.render.pack import render_pack

    run_id, _ = stock_run
    quote, sh, acts = market()

    async def fake_market(symbol):
        return {"quote": quote, "shareholding": sh, "actions": acts, "summary": {"last_price": "1003.2"},
                "accessed_at": datetime(2026, 9, 29, tzinfo=UTC)}  # fmt: skip

    monkeypatch.setattr(stock_mod, "fetch_market", fake_market)
    runner = StockRunner(run_id)
    pipe = pipeline_for(run_id, runner=runner, tracker=LimitTracker(tmp_path / "limits"),
                        config=PipelineConfig(concurrency=3, render=False))  # fmt: skip
    assert type(pipe).__name__ == "StockPipeline"
    assert await pipe.run() == "done"
    assert set(STOCK_STREAMS) <= set(runner.calls) and "planner" not in runner.calls
    assert {"stock_planner", "stock_bull", "stock_bear", "stock_synthesizer", "stock_critic"} <= set(
        runner.calls
    )
    with session_scope() as s:
        facts = {c.metric: c for c in s.query(Claim).filter_by(run_id=run_id, stream="facts")}
        m = s.get(ResearchRun, run_id).manifest
        mcap_url = facts["market_cap"].citations[0].url
    assert facts["market_cap"].status == "verified" and mcap_url.endswith("symbol=INFY")
    assert m["kind"] == "stock_report" and m["facts"]["market"]["last_price"] == "1003.2"
    pack = render_pack(run_id, pdf=False)
    names = {p.name for p in pack.path.iterdir()}
    assert {"01_Company_Filings", "04_Governance_and_Events", "05_Valuation_and_Price"} <= names
    assert (pack.path / "02_Financial_Reports" / "stock_fundamentals_section.md").exists()
    assert "stock research report" in (pack.path / "06_Final_Report" / "report.html").read_text()

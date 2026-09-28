"""The listed-stock report: the research pipeline with stock streams, filings and NSE market facts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from finresearch.agents.roles import STOCK_STREAMS
from finresearch.db import session_scope
from finresearch.orchestrator.base import ResearchPipeline

STOCK_PIPELINE_VERSION = "stock-pipeline-1"


class StockPipeline(ResearchPipeline):
    kind = "stock_report"
    version = STOCK_PIPELINE_VERSION
    default_streams = STOCK_STREAMS
    roles = {"planner": "stock_planner", "verifier": "verifier", "bull": "stock_bull",  # noqa: RUF012
             "bear": "stock_bear", "synthesizer": "stock_synthesizer", "critic": "stock_critic"}  # fmt: skip
    required_doc_kinds = ("ANNUAL_REPORT",)
    discovery_kind = "stock"
    subject = "a listed Indian stock"
    primary_source = "the company's filings (annual reports and results filings)"
    decision_deadline = "the investor's next review (before the next quarterly results)"

    async def _facts(self) -> None:
        from finresearch.fincalc.dates import today_ist

        facts = dict(self.ctx.facts)
        facts["today_ist"] = today_ist().isoformat()
        if self.ctx.nse_symbol and "market" not in facts:
            try:
                market = await fetch_market(self.ctx.nse_symbol)
                facts["market"] = market["summary"]
                self._once("stock_baseline", lambda: self._record(market, facts))
            except Exception as e:
                facts["market_error"] = f"{type(e).__name__}: {e}"
        self.ctx.facts = facts
        self._update_manifest(facts=facts)

    def _record(self, market: dict[str, Any], facts: dict[str, Any]) -> None:
        from finresearch.fincalc.dates import today_ist
        from finresearch.verify.stock_baseline import record_stock_facts, stock_facts

        items = stock_facts(self.ctx.nse_symbol, market["quote"], market["shareholding"], market["actions"],
                            today_ist())  # fmt: skip
        with session_scope() as s:
            facts["baseline_claim_ids"] = record_stock_facts(s, self.run_id, items, market["accessed_at"])


async def fetch_market(symbol: str) -> dict[str, Any]:
    from finresearch.adapters.nse import NseClient
    from finresearch.adapters.nse_equity import NseEquity

    async with NseClient() as nse:
        quote = await nse.quote(symbol)
    async with NseEquity() as eq:
        shareholding = await eq.shareholding(symbol)
        actions = await eq.corporate_actions(symbol)
    summary = {"last_price": str(quote.last_price), "as_of": quote.as_of.isoformat() if quote.as_of else None,
               "week52_high": str(quote.week52_high), "week52_low": str(quote.week52_low),
               "industry": quote.industry,
               "promoter_holding": str(shareholding[0].promoter_pct) if shareholding else None}  # fmt: skip
    return {"quote": quote, "shareholding": shareholding, "actions": actions, "summary": summary,
            "accessed_at": datetime.now(UTC)}  # fmt: skip

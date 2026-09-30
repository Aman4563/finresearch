"""The listed-stock report: the research pipeline with stock streams, filings and exchange market facts (NSE, or BSE
for a BSE-only stock: quote, 52-week range, market cap, shareholding, corporate actions and the latest results from
the filing's XBRL)."""

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
        bse_code = None if self.ctx.nse_symbol else self.ctx.bse_code
        if (self.ctx.nse_symbol or bse_code) and "market" not in facts:
            try:
                market = await (
                    fetch_market(self.ctx.nse_symbol) if self.ctx.nse_symbol else fetch_market_bse(bse_code)
                )
                facts["market"] = market["summary"]
                self._once("stock_baseline", lambda: self._record(market, facts))
            except Exception as e:
                facts["market_error"] = f"{type(e).__name__}: {e}"
        self.ctx.facts = facts
        self._update_manifest(facts=facts)

    def _record(self, market: dict[str, Any], facts: dict[str, Any]) -> None:
        from finresearch.fincalc.dates import today_ist
        from finresearch.verify.stock_baseline import record_stock_facts, stock_facts

        exchange = market.get("exchange", "NSE")
        key = self.ctx.nse_symbol if exchange == "NSE" else f"BSE:{self.ctx.bse_code}"
        items = stock_facts(key, market["quote"], market["shareholding"], market["actions"], today_ist(),
                            exchange=exchange, results=market.get("results"))  # fmt: skip
        with session_scope() as s:
            facts["baseline_claim_ids"] = record_stock_facts(s, self.run_id, items, market["accessed_at"],
                                                             exchange=exchange)  # fmt: skip


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


async def fetch_market_bse(code: str, *, equity: Any = None) -> dict[str, Any]:
    """The same market facts for a BSE-only stock, from BSE (adapters/bse_equity.py): the quote (last price, 52-week
    range, BSE's own market cap, ISIN), shareholding (latest promoter % from BSE's summary), corporate actions, and the
    latest quarter's results read from its Integrated Filing XBRL (revenue, net profit, EPS)."""
    from finresearch.adapters.bse_equity import BseEquity
    from finresearch.api.markets import results_from_nse

    async with equity or BseEquity() as eq:
        quote = await eq.quote(code)
        if quote is None:
            raise LookupError(f"BSE has no quote for scrip {code}")
        shareholding = await eq.shareholding(code)
        actions = await eq.corporate_actions(code)
        try:
            results = await results_from_nse(eq, code, 4)
        except Exception as e:  # results are a bonus: the quote and holdings still make the baseline
            results = {"quarters": [], "errors": [f"{type(e).__name__}: {e}"[:200]]}
    latest = next((sh for sh in shareholding if sh.promoter_pct is not None), None)
    last_q = (results.get("quarters") or [None])[-1]
    summary = {"exchange": "BSE", "bse_code": code, "isin": quote.isin, "last_price": str(quote.last_price),
               "as_of": quote.as_of.isoformat() if quote.as_of else None,
               "week52_high": str(quote.week52_high), "week52_low": str(quote.week52_low),
               "market_cap": str(quote.market_cap) if quote.market_cap is not None else None,
               "industry": quote.industry, "group": quote.group,
               "promoter_holding": str(latest.promoter_pct) if latest else None,
               "latest_quarter": {k: last_q.get(k) for k in ("label", "period_end", "revenue", "profit", "eps",
                                                              "consolidated", "xbrl")} if last_q else None}  # fmt: skip
    return {"exchange": "BSE", "quote": quote, "shareholding": shareholding, "actions": actions, "results": results,
            "summary": summary, "accessed_at": datetime.now(UTC)}  # fmt: skip

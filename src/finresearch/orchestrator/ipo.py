"""The IPO report: the research pipeline with the IPO's streams, offer documents and NSE (or BSE SME) issue facts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from finresearch.agents.roles import STREAMS
from finresearch.db import session_scope
from finresearch.orchestrator.base import (  # re-exported: the IPO pipeline was the first and only kind
    DEFAULT_COST,
    PipelineConfig,
    ResearchPipeline,
    RunPaused,
    StepFailed,
    _now,
    create_run,
    run_until_done,
)

PIPELINE_VERSION = "ipo-pipeline-1"

__all__ = ["DEFAULT_COST", "PIPELINE_VERSION", "IpoPipeline", "PipelineConfig", "RunPaused", "StepFailed",
           "create_run", "run_until_done"]  # fmt: skip


class IpoPipeline(ResearchPipeline):
    kind = "ipo_report"
    version = PIPELINE_VERSION
    default_streams = STREAMS
    required_doc_kinds = ("RHP", "DRHP")
    subject = "an Indian IPO"
    primary_source = "the offer documents (the RHP text layer)"

    async def _facts(self) -> None:
        from finresearch.fincalc.dates import bidding_day_number, today_ist

        facts = dict(self.ctx.facts)
        bse_ipo_no = None if self.ctx.nse_symbol else self._bse_ipo_no()
        if (self.ctx.nse_symbol or bse_ipo_no) and "issue_info" not in facts:
            try:
                if self.ctx.nse_symbol:
                    from finresearch.mcp_server.server import nse_ipo_detail

                    d = json.loads(await nse_ipo_detail(self.ctx.nse_symbol))
                    facts["issue_info"] = d.get("issue_info", {})
                    facts["nse_fetched_at"] = d.get("fetched_at")
                else:  # a BSE SME issue: BSE is the only exchange publishing its details
                    from finresearch.adapters.bse import BseClient

                    async with BseClient() as bse:
                        bd = await bse.issue_detail(bse_ipo_no)
                    facts["issue_info"] = bd.issue_info()
                    facts["bse_fetched_at"] = bd.fetch.fetched_at.isoformat() if bd.fetch else None
                    facts["exchange"] = "BSE"
                self._once("baseline", lambda: self._record_baseline(facts))
                period = facts["issue_info"].get("Issue Period", "")
                if " to " in period:
                    open_s, close_s = (x.strip() for x in period.split(" to "))
                    op = datetime.strptime(open_s, "%d-%b-%Y").date()
                    cl = datetime.strptime(close_s, "%d-%b-%Y").date()
                    facts["issue_open"], facts["issue_close"] = op.isoformat(), cl.isoformat()
                    from finresearch.adapters.nse_holidays import trading_holidays

                    facts["bidding_day_today"] = bidding_day_number(op, today_ist(), trading_holidays())
            except Exception as e:
                facts["issue_info_error"] = f"{type(e).__name__}: {e}"
        facts["today_ist"] = today_ist().isoformat()
        self.ctx.facts = facts
        if facts.get("issue_close"):
            self.ctx.decision_deadline = f"the UPI mandate cut-off, 5:00 PM IST on {facts['issue_close']}"
        self._update_manifest(facts=facts)

    def _bse_ipo_no(self) -> int | None:
        from finresearch.db.models import Company, ResearchRun

        with session_scope() as s:
            run = s.get(ResearchRun, self.run_id)
            co = s.get(Company, run.company_id) if run and run.company_id else None
            return (co.meta or {}).get("bse_ipo_no") if co else None

    def _record_baseline(self, facts: dict[str, Any]) -> None:
        from finresearch.verify.baseline import record_baseline

        if facts.get("exchange") == "BSE":
            from finresearch.adapters.bse import detail_url

            fetched = facts.get("bse_fetched_at")
            extra = {"exchange": "BSE", "url": detail_url(facts["issue_info"]["BSE IPO No"])}
        else:
            fetched, extra = facts.get("nse_fetched_at"), {}
        with session_scope() as s:
            ids = record_baseline(s, self.run_id, self.ctx.nse_symbol, facts.get("issue_info", {}),
                                  datetime.fromisoformat(fetched) if fetched else _now(), **extra)  # fmt: skip
        facts["baseline_claim_ids"] = ids

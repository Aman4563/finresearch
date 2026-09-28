"""The IPO report: the research pipeline with the IPO's streams, offer documents and NSE issue facts."""

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

    async def _facts(self) -> None:
        from finresearch.fincalc.dates import bidding_day_number, today_ist

        facts = dict(self.ctx.facts)
        if self.ctx.nse_symbol and "issue_info" not in facts:
            try:
                from finresearch.mcp_server.server import nse_ipo_detail

                d = json.loads(await nse_ipo_detail(self.ctx.nse_symbol))
                facts["issue_info"] = d.get("issue_info", {})
                facts["nse_fetched_at"] = d.get("fetched_at")
                self._once("baseline", lambda: self._record_baseline(facts))
                period = facts["issue_info"].get("Issue Period", "")
                if " to " in period:
                    open_s, close_s = (x.strip() for x in period.split(" to "))
                    op = datetime.strptime(open_s, "%d-%b-%Y").date()
                    cl = datetime.strptime(close_s, "%d-%b-%Y").date()
                    facts["issue_open"], facts["issue_close"] = op.isoformat(), cl.isoformat()
                    facts["bidding_day_today"] = bidding_day_number(op, today_ist())
            except Exception as e:
                facts["issue_info_error"] = f"{type(e).__name__}: {e}"
        facts["today_ist"] = today_ist().isoformat()
        self.ctx.facts = facts
        if facts.get("issue_close"):
            self.ctx.decision_deadline = f"the UPI mandate cut-off, 5:00 PM IST on {facts['issue_close']}"
        self._update_manifest(facts=facts)

    def _record_baseline(self, facts: dict[str, Any]) -> None:
        from finresearch.verify.baseline import record_baseline

        fetched = facts.get("nse_fetched_at")
        with session_scope() as s:
            ids = record_baseline(s, self.run_id, self.ctx.nse_symbol, facts.get("issue_info", {}),
                                  datetime.fromisoformat(fetched) if fetched else _now())  # fmt: skip
        facts["baseline_claim_ids"] = ids

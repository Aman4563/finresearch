"""Live integration: Claude Code (Max) -> FinResearch MCP tools -> claim ledger, verified from the DB.

Requires: Orient Cables RHP ingested (document with company slug 'orient-cables').
"""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import select

from finresearch.bridge import AgentTask, ModelClass, Tier, build_router
from finresearch.config import get_settings
from finresearch.db import session_scope
from finresearch.db.models import Claim, Company, ResearchRun
from finresearch.mcp_server.config import ALLOW_ALL, write_mcp_config

SCHEMA = {
    "type": "object",
    "properties": {
        "largest_customer_share_pct": {"type": "number"},
        "page": {"type": "integer"},
        "line": {"type": "integer"},
        "claim_id": {"type": "integer"},
        "quote_found": {"type": "boolean"},
    },
    "required": ["largest_customer_share_pct", "page", "line", "claim_id", "quote_found"],
}


async def main() -> int:
    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.slug == "orient-cables"))
        run = ResearchRun(company_id=co.id if co else None, kind="smoke_mcp")
        s.add(run)
        s.flush()
        run_id = run.id
    prompt = f"""You are a financial research agent working ONLY through the `finresearch` MCP tools.
Task: For Orient Cables (India) Limited, find what percentage of revenue from operations came from the LARGEST
customer in the three months ended June 30, 2026 (Q1 FY27), according to the RHP.
Steps: list_documents -> locate the relevant table with grep_document or search_documents -> read the exact lines
with read_lines_tool -> call save_claim(run_id={run_id}, stream="business", claim_type="numeric",
metric="largest_customer_share", value=<number>, unit="% of revenue from operations", period="Q1 FY27",
importance="high", citations=[{{"document_id": <id>, "line_start": <line>, "line_end": <line>,
"quote": "<text copied exactly from that line>"}}]). If quote_found is false, fix the citation and save again.
Finally return the JSON result."""
    task = AgentTask(
        name="smoke-mcp-claim",
        prompt=prompt,
        json_schema=SCHEMA,
        model_class=ModelClass.STANDARD,
        effort="low",
        allowed_tools=[ALLOW_ALL],
        mcp_config=write_mcp_config(),
        max_turns=20,
        timeout_s=600,
        allow_degraded=False,  # tool-using research must not fall back to local
        run_dir=get_settings().runs_dir / "smoke_mcp",
    )
    res = await build_router().run(task, force_tier=Tier.CLAUDE_MAX)
    print(f"tier={res.tier.value} model={res.model} turns={res.num_turns} {res.duration_s:.0f}s "
          f"est=${res.cost_usd_estimate:.3f} warnings={res.warnings}")  # fmt: skip
    print("agent output:", json.dumps(res.structured_output))
    with session_scope() as s:
        claims = s.scalars(select(Claim).where(Claim.run_id == run_id)).all()
        for c in claims:
            print(f"DB claim {c.id}: status={c.status} value={c.value} unit={c.unit} period={c.period}")
            for ct in c.citations:
                print(f"   cite doc={ct.document_id} p{ct.page_no} L{ct.line_start}-{ct.line_end} "
                      f"quote_found={ct.quote_found} quote={ct.quote!r}")  # fmt: skip
        good = [c for c in claims if c.value is not None and abs(float(c.value) - 38.54) < 0.01
                and any(ct.quote_found for ct in c.citations)]  # fmt: skip
    print("PASS" if good else "FAIL: no verified claim with value 38.54")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

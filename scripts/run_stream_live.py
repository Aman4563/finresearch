"""Live run of one research role on an ingested company (acceptance check for the agent definitions).

usage: uv run python scripts/run_stream_live.py <company-slug> <role> [--run-id N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from sqlalchemy import func, select

from finresearch.agents.runner import RunContext, run_role
from finresearch.db import session_scope
from finresearch.db.models import Citation, Claim, Company, ResearchRun
from finresearch.mcp_server.server import list_documents


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("role")
    ap.add_argument("--run-id", type=int)
    a = ap.parse_args()
    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.slug == a.company))
        if co is None:
            print(f"unknown company {a.company}")
            return 2
        run_id = a.run_id
        if run_id is None:
            run = ResearchRun(company_id=co.id, kind="stream_acceptance", manifest={"role": a.role})
            s.add(run)
            s.flush()
            run_id = run.id
        name, sym = co.name, co.nse_symbol
    docs = json.loads(list_documents(a.company))
    ctx = RunContext(run_id=run_id, company_slug=a.company, company_name=name, nse_symbol=sym, documents=docs)
    t0 = time.time()
    parsed, res = await run_role(a.role, ctx)
    print(f"role={a.role} run={run_id} tier={res.tier.value} model={res.model} turns={res.num_turns} "
          f"{time.time() - t0:.0f}s est=${res.cost_usd_estimate:.2f} warnings={res.warnings}")  # fmt: skip
    if res.rate_limit:
        print(f"5h window {res.rate_limit.five_hour_utilization}, 7d {res.rate_limit.seven_day_utilization}")
    print(json.dumps(parsed.model_dump(), indent=1)[:6000])
    with session_scope() as s:
        rows = s.execute(
            select(Claim.status, func.count()).where(Claim.run_id == run_id).group_by(Claim.status)
        ).all()
        cites = s.execute(select(Citation.quote_found, func.count()).join(Claim)
                          .where(Claim.run_id == run_id).group_by(Citation.quote_found)).all()  # fmt: skip
    print("claims by status:", dict(rows), "| citations quote_found:", {str(k): v for k, v in cites})
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

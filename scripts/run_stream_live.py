"""Live run of one research role on an ingested company (acceptance check for the agent definitions). A BSE-only
stock (company with a bse_code and no NSE symbol) runs as "BSE:<scrip code>"; stock_* roles get the stock pipeline's
context (subject, primary source, market facts).

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
        name, sym, bse_code = co.name, co.nse_symbol, co.bse_code
    if a.role.startswith("stock_") and not sym and not bse_code:
        # without either key the prompts would say "NSE n/a" and the agent would guess a ticker
        print(f"{a.company} has neither an NSE symbol nor a BSE code; set one before running a stock role")
        return 2
    docs = json.loads(list_documents(a.company))
    extra: dict = {}
    # stock_* roles: the listed-stock pipeline's context (subject, primary source, market facts)
    if a.role.startswith("stock_"):
        from finresearch.orchestrator.stock import StockPipeline, fetch_market, fetch_market_bse

        extra = {"subject": StockPipeline.subject, "primary_source": StockPipeline.primary_source,
                 "decision_deadline": StockPipeline.decision_deadline}  # fmt: skip
        try:
            market = await (fetch_market(sym) if sym else fetch_market_bse(bse_code))
            extra["facts"] = {"market": market["summary"]}
        except Exception as e:
            extra["facts"] = {"market_error": f"{type(e).__name__}: {e}"}
    # a BSE-only stock (no NSE symbol) is keyed "BSE:<scrip code>" in the prompts and the equity tools
    ctx = RunContext(run_id=run_id, company_slug=a.company, company_name=name, nse_symbol=sym,
                     bse_code=None if sym else bse_code, documents=docs, **extra)  # fmt: skip
    print(f"company {a.company}: {ctx.listing()}")
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

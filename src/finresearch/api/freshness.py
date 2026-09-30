"""'Since this report' (docs/dev/RESEARCH_ROADMAP.md item 4): how old a report is, where the live price sits against
the report's entry zone and fair-value bands, which filings came in since, and whether a re-run is worth it.

GET only; the live price and announcements come from NSE (BSE, for a BSE-only stock) through the same sources as the stock pages
(`app.state.markets` is the test seam), the bands from the report's own ledger claims (insights.fair_values).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from finresearch.api.markets import MarketSources, TtlCache


def add_freshness_routes(app: FastAPI, *, clock=None) -> None:
    now = clock or (lambda: datetime.now(UTC))
    cache = TtlCache()

    def src() -> MarketSources:
        s = getattr(app.state, "markets", None)
        if s is None:
            s = app.state.markets = MarketSources()
        return s

    def _run_facts(run_id: int) -> dict[str, Any]:
        from finresearch.api.app import claim_json
        from finresearch.api.insights import _claims, fair_values
        from finresearch.db import session_scope
        from finresearch.db.models import Claim, Company, ResearchRun

        with session_scope() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                raise HTTPException(404, f"unknown run {run_id}")
            co = s.get(Company, run.company_id) if run.company_id else None
            claims = [claim_json(c, {}) for c in s.scalars(select(Claim).where(Claim.run_id == run_id)).all()]
            usable, _ = _claims(claims)
            out = {"run_id": run.id, "kind": run.kind, "status": run.status, "symbol": co.stock_key if co else None,
                   "report_at": run.finished_at or run.created_at, "fair": fair_values(usable)}  # fmt: skip
            s.rollback()
            return out

    async def _build(facts: dict[str, Any]) -> dict[str, Any]:
        from finresearch.api.insights import freshness

        sym = facts["symbol"]
        price = as_of = None
        anns: list[dict[str, Any]] = []
        errors: list[str] = []
        from finresearch.adapters.bse_equity import scrip_code_of

        code = scrip_code_of(sym or "")
        ex, sid = ("BSE", code) if code else ("NSE", sym)
        if sym:
            try:
                q = await src().get_quote(sid, ex) if code else await src().get_quote(sid)
                last = q.last_price or q.close_price
                price = float(last) if last is not None else None
                as_of = q.as_of.isoformat() if q.as_of else None
            except Exception as e:
                errors.append(f"quote: {type(e).__name__}")
            try:
                async with src().open_equity(ex) if code else src().open_equity() as eq:
                    anns = [{"at": a.at.isoformat() if a.at else None, "category": a.category, "text": a.text[:300],
                             "attachment": a.attachment,
                             "results_period_end": a.results_period_end.isoformat() if a.results_period_end else None}
                            for a in await eq.announcements(sid)]  # fmt: skip
            except Exception as e:
                errors.append(f"announcements: {type(e).__name__}")
        out = freshness(run_id=facts["run_id"], kind=facts["kind"], symbol=sym, report_at=facts["report_at"],
                        now=now(), price=price, price_as_of=as_of, fair=facts["fair"], announcements=anns)  # fmt: skip
        out["errors"] = errors
        return out

    @app.get("/api/runs/{run_id}/since")
    async def run_since(run_id: int) -> dict[str, Any]:
        """Days since the report, live price vs the report's bands, filings since, and a re-run hint."""

        # the ledger read is synchronous and heavy (every claim of the run, ~2 s for a 376-claim stock report): run it in
        # a worker thread and only on a cache miss, so it neither repeats per request nor blocks the event loop that
        # serves the page's other requests (docs/dev/RESEARCH_ROADMAP.md §B0.1, measured stall of every call on
        # /stocks/INFY)
        async def make() -> dict[str, Any]:
            return await _build(await asyncio.to_thread(_run_facts, run_id))

        return await cache.get(("since", run_id), 300, make)

    @app.get("/api/stocks/{symbol}/since-report")
    async def stock_since(symbol: str) -> dict[str, Any]:
        """The same for the latest finished stock report on this symbol; `run_id` is null when there is none."""
        from finresearch.db import session_scope
        from finresearch.db.models import Company, ResearchRun
        from finresearch.signals.stock import instrument_key

        try:
            sym = instrument_key(symbol)  # an NSE symbol, or "BSE:<code>" for a BSE-only stock
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        code = sym[4:] if sym.startswith("BSE:") else None
        match = (
            (Company.bse_code == code) & Company.nse_symbol.is_(None) if code else Company.nse_symbol == sym
        )
        with session_scope() as s:
            run_id = s.scalar(select(ResearchRun.id).join(Company, Company.id == ResearchRun.company_id)
                              .where(match, ResearchRun.kind == "stock_report",
                                     ResearchRun.status == "done")
                              .order_by(ResearchRun.finished_at.desc().nulls_last(), ResearchRun.id.desc()).limit(1))  # fmt: skip
        if run_id is None:
            return {"run_id": None, "symbol": sym}
        return await run_since(run_id)
